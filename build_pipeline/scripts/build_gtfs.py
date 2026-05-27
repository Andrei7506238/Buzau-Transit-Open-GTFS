"""
build_pipeline/scripts/build_gtfs.py
====================================
Build a GTFS feed from the Buzău county transport data.

Reads:
  parsed/lista_statiilor.json    - route station lists with km distances
  parsed/program_transport.json  - schedule entries (times + days)
  station_finder/stops_geocoded.json - geocoded stop coordinates
  station_finder/canonical_map.json  - variant-name → canonical-name map

Writes to build_pipeline/output/gtfs/:
  agency.txt, stops.txt, routes.txt, trips.txt,
  stop_times.txt, calendar.txt
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
PARSED = ROOT / "parsed"
STATION_FINDER = ROOT / "station_finder"
GTFS_OUT = ROOT / "build_pipeline" / "output" / "gtfs"
BANNED_ROUTES_PATH = ROOT / "banned_routes.json"

AGENCY_ID = "BUZAU"
AGENCY_NAME = "Consiliul Județean Buzău - Transport județean"
AGENCY_URL = "https://www.cjbuzau.ro"
AGENCY_TIMEZONE = "Europe/Bucharest"
AGENCY_LANG = "ro"

FEED_PUBLISHER_NAME = "Popa Andrei-Robert"
FEED_PUBLISHER_URL = "https://andrei7506238.github.io/"
FEED_VERSION = "2026.05-beta.1"

# GTFS calendar date range (published validity window runs through 2028)
CAL_START = "20260501"
CAL_END = "20280531"
# School-year range (Romanian school years covered by the published feed)
SCH_START = "20260909"
SCH_END = "20280613"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_minutes(t: str | None) -> int | None:
    """Parse 'HH:MM' → integer minutes since midnight. Returns None if t is None."""
    if not t:
        return None
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _gtfs_time(minutes: int) -> str:
    """Convert minutes-since-midnight to GTFS HH:MM:SS (may exceed 24:00)."""
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}:00"


def _day_key(days: list[int], school: bool) -> str:
    """Unique key for a service pattern."""
    key = "".join(str(d) for d in sorted(days))
    return key + "_SCH" if school else key


def _day_flags(days: list[int]) -> dict[str, str]:
    """Build GTFS calendar day-flag dict from ISO day list (1=Mon, 7=Sun)."""
    iso = set(days)
    return {
        "monday":    "1" if 1 in iso else "0",
        "tuesday":   "1" if 2 in iso else "0",
        "wednesday": "1" if 3 in iso else "0",
        "thursday":  "1" if 4 in iso else "0",
        "friday":    "1" if 5 in iso else "0",
        "saturday":  "1" if 6 in iso else "0",
        "sunday":    "1" if 7 in iso else "0",
    }


# ---------------------------------------------------------------------------
# Stop coordinate resolution
# ---------------------------------------------------------------------------

def resolve_stop_coords(
    station_names: list[str],
    cmap: dict[str, str],
    geocoded: dict[str, dict],
) -> list[tuple[str, float, float]]:
    """
    For each station name in a route's ordered list, return
    (canonical_name, lat, lon).  Missing stops are filled by propagating
    coordinates from the nearest geocoded neighbour.
    """
    canonicals = [cmap.get(n, n) for n in station_names]
    coords: list[tuple[float, float] | None] = [
        (geocoded[c]["lat"], geocoded[c]["lon"]) if c in geocoded else None
        for c in canonicals
    ]

    # Forward propagation
    last = None
    for i, c in enumerate(coords):
        if c is not None:
            last = c
        elif last is not None:
            coords[i] = last  # type: ignore[assignment]

    # Backward propagation (for leading missing stops)
    last = None
    for i in range(len(coords) - 1, -1, -1):
        if coords[i] is not None:
            last = coords[i]
        elif last is not None:
            coords[i] = last  # type: ignore[assignment]

    result = []
    for i, c in enumerate(canonicals):
        if coords[i] is None:
            # Still None - route has zero geocoded stops; use (0, 0) as sentinel
            result.append((c, 0.0, 0.0))
        else:
            result.append((c, coords[i][0], coords[i][1]))  # type: ignore[index]
    return result


# ---------------------------------------------------------------------------
# Stop time builder
# ---------------------------------------------------------------------------

def build_stop_times(
    station_names: list[str],
    km_values: list[float],
    dep_min: int,
    arr_min: int,
    trip_id: str,
    stop_name_to_id: dict[str, str],
    reverse: bool = False,
) -> list[dict]:
    """
    Build stop_times rows for one trip leg.
    km_values[i] = km from previous station (first entry is 0.0).
    dep_min / arr_min = departure/arrival minutes since midnight.
    If reverse=True, the station sequence is travelled backwards.

    GTFS permits intermediate stops without times, so we only emit explicit
    times for the first and last stop (anchor timetable points).
    """
    # km_values is retained in the signature for compatibility with the
    # existing call sites and future use, but stop timing is now anchor-only.
    _ = km_values

    names = list(station_names)
    if reverse:
        names = list(reversed(names))

    rows = []
    first_time = _gtfs_time(dep_min)
    last_time = _gtfs_time(arr_min)

    for seq, name in enumerate(names, start=1):
        is_first = seq == 1
        is_last = seq == len(names)

        if is_first:
            arrival_time = first_time
            departure_time = first_time
        elif is_last:
            arrival_time = last_time
            departure_time = last_time
        else:
            arrival_time = ""
            departure_time = ""

        stop_id = stop_name_to_id.get(name, "UNKNOWN")
        rows.append({
            "trip_id": trip_id,
            "arrival_time": arrival_time,
            "departure_time": departure_time,
            "stop_id": stop_id,
            "stop_sequence": seq,
        })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Loading data …")
    lista   = json.loads((PARSED / "lista_statiilor.json").read_text(encoding="utf-8"))
    prog    = json.loads((PARSED / "program_transport.json").read_text(encoding="utf-8"))
    geocoded = json.loads((STATION_FINDER / "stops_geocoded.json").read_text(encoding="utf-8"))
    cmap    = json.loads((STATION_FINDER / "canonical_map.json").read_text(encoding="utf-8"))

    # Load and apply route ban-list
    banned: set[str] = set()
    if BANNED_ROUTES_PATH.exists():
        ban_data = json.loads(BANNED_ROUTES_PATH.read_text(encoding="utf-8"))
        banned = {str(r) for r in ban_data.get("banned", [])}
    if banned:
        lista = [r for r in lista if str(r["route_number"]) not in banned]
        prog  = [p for p in prog  if str(p["route_number"]) not in banned]
        print(f"  Banned routes excluded: {sorted(banned)}")

    # Index program_transport by route_number
    prog_by_route: dict[str, dict] = {p["route_number"]: p for p in prog}

    # ---------------------------------------------------------------------------
    # Build global stop registry
    # ---------------------------------------------------------------------------
    print("Building stop registry …")
    # Collect all (station_name, route) pairs → resolved coords
    # Key: canonical_name → {lat, lon}
    global_stops: dict[str, tuple[float, float]] = {}  # canonical → (lat, lon)

    for route in lista:
        station_names = [s["station_name"] for s in route["stations"]]
        resolved = resolve_stop_coords(station_names, cmap, geocoded)
        for canon, lat, lon in resolved:
            if canon not in global_stops:
                global_stops[canon] = (lat, lon)
            # else: already known, keep first resolution

    # Assign numeric stop_ids
    stop_id_map: dict[str, str] = {}
    stops_rows: list[dict] = []
    for idx, (canon, (lat, lon)) in enumerate(sorted(global_stops.items()), start=1):
        sid = f"s{idx:04d}"
        stop_id_map[canon] = sid
        stops_rows.append({
            "stop_id": sid,
            "stop_name": canon,
            "stop_lat": f"{lat:.6f}",
            "stop_lon": f"{lon:.6f}",
        })

    print(f"  {len(stops_rows)} unique stops (canonical names)")

    # ---------------------------------------------------------------------------
    # Calendar (unique service patterns)
    # ---------------------------------------------------------------------------
    service_rows: list[dict] = []
    seen_services: set[str] = set()

    def ensure_service(days: list[int], school: bool) -> str:
        key = _day_key(days, school)
        if key not in seen_services:
            seen_services.add(key)
            start = SCH_START if school else CAL_START
            end   = SCH_END   if school else CAL_END
            flags = _day_flags(days)
            service_rows.append({
                "service_id": key,
                **flags,
                "start_date": start,
                "end_date": end,
            })
        return key

    # ---------------------------------------------------------------------------
    # Trips + stop_times
    # ---------------------------------------------------------------------------
    trips_rows: list[dict] = []
    stop_times_rows: list[dict] = []

    total_trips = 0

    for route in lista:
        rnum = route["route_number"]
        station_names_raw = [s["station_name"] for s in route["stations"]]
        km_values = [s["kilometers_from_previous_station"] for s in route["stations"]]

        # Resolve canonical names for the entire station sequence
        resolved = resolve_stop_coords(station_names_raw, cmap, geocoded)
        canon_names = [r[0] for r in resolved]

        prog_entry = prog_by_route.get(rnum, {})
        schedule_entries = prog_entry.get("schedule_entries", [])

        for entry_idx, entry in enumerate(schedule_entries):
            days  = entry.get("days_of_operation", [])
            school = entry.get("only_for_school_days", False)
            svc_id = ensure_service(days, school)

            way_dep = _parse_minutes(entry.get("way_departure"))
            way_arr = _parse_minutes(entry.get("way_arrival"))
            back_dep = _parse_minutes(entry.get("back_departure"))
            back_arr = _parse_minutes(entry.get("back_arrival"))

            # Handle overnight arrivals: if arrival < departure, it's next-day
            if way_dep is not None and way_arr is not None and way_arr < way_dep:
                way_arr += 24 * 60

            if back_dep is not None and back_arr is not None and back_arr < back_dep:
                back_arr += 24 * 60

            # --- Outbound (way) trip ---
            if way_dep is not None and way_arr is not None:
                trip_id = f"r{rnum}_e{entry_idx:02d}_w"
                headsign = route.get("to_location", canon_names[-1] if canon_names else "")
                trips_rows.append({
                    "route_id": rnum,
                    "service_id": svc_id,
                    "trip_id": trip_id,
                    "trip_headsign": headsign,
                    "direction_id": "0",
                })
                stop_times_rows.extend(
                    build_stop_times(
                        canon_names, km_values, way_dep, way_arr,
                        trip_id, stop_id_map, reverse=False,
                    )
                )
                total_trips += 1

            # --- Return (back) trip ---
            if back_dep is not None and back_arr is not None:
                trip_id = f"r{rnum}_e{entry_idx:02d}_b"
                headsign = route.get("from_location", canon_names[0] if canon_names else "")
                trips_rows.append({
                    "route_id": rnum,
                    "service_id": svc_id,
                    "trip_id": trip_id,
                    "trip_headsign": headsign,
                    "direction_id": "1",
                })
                stop_times_rows.extend(
                    build_stop_times(
                        canon_names, km_values, back_dep, back_arr,
                        trip_id, stop_id_map, reverse=True,
                    )
                )
                total_trips += 1

    print(f"  {total_trips} trips, {len(stop_times_rows)} stop_time rows")

    # ---------------------------------------------------------------------------
    # Routes
    # ---------------------------------------------------------------------------
    routes_rows: list[dict] = []
    lista_by_route = {r["route_number"]: r for r in lista}
    prog_by_route2 = {p["route_number"]: p for p in prog}
    for rnum in sorted(lista_by_route.keys()):
        r = lista_by_route[rnum]
        p = prog_by_route2.get(rnum, {})
        routes_rows.append({
            "route_id": rnum,
            "agency_id": AGENCY_ID,
            "route_short_name": rnum,
            "route_long_name": f"{r.get('from_location','')} - {r.get('to_location','')}",
            "route_type": "3",   # Bus
        })

    # ---------------------------------------------------------------------------
    # Write GTFS files
    # ---------------------------------------------------------------------------
    GTFS_OUT.mkdir(parents=True, exist_ok=True)

    def write_csv(filename: str, fieldnames: list[str], rows: list[dict]) -> None:
        path = GTFS_OUT / filename
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"  Wrote {filename} ({len(rows)} rows)")

    print("\nWriting GTFS files …")

    # agency.txt
    write_csv("agency.txt", [
        "agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang",
    ], [{
        "agency_id": AGENCY_ID,
        "agency_name": AGENCY_NAME,
        "agency_url": AGENCY_URL,
        "agency_timezone": AGENCY_TIMEZONE,
        "agency_lang": AGENCY_LANG,
    }])

    # feed_info.txt
    write_csv("feed_info.txt", [
        "feed_publisher_name", "feed_publisher_url", "feed_lang",
        "feed_start_date", "feed_end_date", "feed_version",
        "feed_contact_url",
    ], [{
        "feed_publisher_name": FEED_PUBLISHER_NAME,
        "feed_publisher_url": FEED_PUBLISHER_URL,
        "feed_lang": AGENCY_LANG,
        "feed_start_date": CAL_START,
        "feed_end_date": CAL_END,
        "feed_version": FEED_VERSION,
        "feed_contact_url": FEED_PUBLISHER_URL,
    }])

    # stops.txt
    write_csv("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"], stops_rows)

    # routes.txt
    write_csv("routes.txt", [
        "route_id", "agency_id", "route_short_name", "route_long_name", "route_type",
    ], routes_rows)

    # calendar.txt
    write_csv("calendar.txt", [
        "service_id", "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday", "start_date", "end_date",
    ], service_rows)

    # trips.txt
    write_csv("trips.txt", [
        "route_id", "service_id", "trip_id", "trip_headsign", "direction_id",
    ], trips_rows)

    # stop_times.txt
    write_csv("stop_times.txt", [
        "trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence",
    ], stop_times_rows)

    print("\nDone! GTFS feed written to", GTFS_OUT)
    print(f"  {len(routes_rows)} routes")
    print(f"  {len(stops_rows)} stops")
    print(f"  {len(service_rows)} service calendars")
    print(f"  {total_trips} trips")
    print(f"  {len(stop_times_rows)} stop_times")


if __name__ == "__main__":
    main()
