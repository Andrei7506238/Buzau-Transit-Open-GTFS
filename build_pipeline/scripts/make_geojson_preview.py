"""
build_pipeline/scripts/make_geojson_preview.py
==============================================
Generate a GeoJSON preview of all geocoded stops AND route paths for
geojson.io visual review.

Output: build_pipeline/output/stops_preview.geojson

Usage:
    python build_pipeline/scripts/make_geojson_preview.py
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
GTFS = ROOT / "build_pipeline" / "output" / "gtfs"


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Stop point features (coloured by geocoding source)
# ---------------------------------------------------------------------------

geo = json.loads((ROOT / "station_finder" / "stops_geocoded.json").read_text(encoding="utf-8"))

SOURCE_COLORS = {
    "transbus":            "#ff0000",  # red    - Transbus GTFS
    "osm":                 "#00aa00",  # green  - OSM node
    "nominatim":           "#0066ff",  # blue   - Nominatim API
    "manual":              "#ff8800",  # orange - manual coords
    "osrm_interpolated":   "#aa00aa",  # purple - OSRM road interpolation
    "route_interpolated":  "#00cccc",  # teal   - Phase 4 outlier repair
}

# stop_id → (lat, lon) from the generated stops.txt
stop_coords: dict[str, tuple[float, float]] = {
    r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"]))
    for r in _read_csv(GTFS / "stops.txt")
}

stop_features = []
for name, v in sorted(geo.items()):
    lat, lon = v["lat"], v["lon"]
    src = v.get("source", "unknown")
    color = SOURCE_COLORS.get(src, "#888888")
    stop_features.append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "name": name,
            "source": src,
            "marker-color": color,
            "marker-size": "small",
        },
    })

# ---------------------------------------------------------------------------
# Route path features (one LineString per route, direction 0)
# ---------------------------------------------------------------------------

# Cycling palette for route lines
ROUTE_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#9a6324", "#469990", "#800000",
    "#808000", "#dcbeff", "#aaffc3", "#fabed4", "#bfef45",
]

routes_info = {r["route_id"]: r for r in _read_csv(GTFS / "routes.txt")}

# Pick the first direction-0 trip (or first overall) per route
route_rep: dict[str, str] = {}
for row in _read_csv(GTFS / "trips.txt"):
    rid = row["route_id"]
    if rid not in route_rep:
        route_rep[rid] = row["trip_id"]
    elif row.get("direction_id", "") == "0":
        route_rep[rid] = row["trip_id"]

rep_set = set(route_rep.values())

# Collect ordered stop sequences for representative trips only
trip_stops: dict[str, list[tuple[int, str]]] = defaultdict(list)
for row in _read_csv(GTFS / "stop_times.txt"):
    if row["trip_id"] in rep_set:
        trip_stops[row["trip_id"]].append((int(row["stop_sequence"]), row["stop_id"]))
for stops in trip_stops.values():
    stops.sort()

# Build LineString features
route_features = []
sorted_routes = sorted(routes_info.keys())
for idx, rid in enumerate(sorted_routes):
    trip_id = route_rep.get(rid)
    if not trip_id:
        continue
    coords = []
    for _, sid in trip_stops.get(trip_id, []):
        if sid in stop_coords:
            lat, lon = stop_coords[sid]
            coords.append([lon, lat])
    if len(coords) < 2:
        continue
    color = ROUTE_PALETTE[idx % len(ROUTE_PALETTE)]
    rinfo = routes_info[rid]
    route_features.append({
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "route_id": rid,
            "route_short_name": rinfo.get("route_short_name", rid),
            "route_long_name": rinfo.get("route_long_name", ""),
            "stroke": color,
            "stroke-width": 2,
            "stroke-opacity": 0.8,
        },
    })

# ---------------------------------------------------------------------------
# Write output  (routes first so stops render on top)
# ---------------------------------------------------------------------------

geojson = {"type": "FeatureCollection", "features": route_features + stop_features}
out = ROOT / "build_pipeline" / "output" / "stops_preview.geojson"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {len(route_features)} route paths + {len(stop_features)} stops → {out}")
print("Open https://geojson.io and drag the file in to inspect.")
