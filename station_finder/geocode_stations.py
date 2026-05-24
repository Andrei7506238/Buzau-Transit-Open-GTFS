"""
station_finder/geocode_stations.py
===================================
Four-phase geocoding for Buzău county bus stops.

Phase 0 - Transbus GTFS matching
    Fuzzy-match canonical station names against the Transbus city-bus GTFS
    stops in transbus_stations/transbus_stops.txt (source: MobilityDatabase
    feed mdb-2106).  Provides high-quality coordinates for stops shared with
    the Buzău city network.

Phase 1 - OSM anchor matching
    Fuzzy-match canonical station names against the named nodes in
    overpass/export.geojson using rapidfuzz.

Phase 2 - Nominatim cache lookup
    Apply pre-computed Nominatim results from nominatim_cache.json.
    To refresh or extend the cache, run:
        python station_finder/nominatim/geocode_nominatim.py

Phase 2b - Manual coordinates (override)
    Always-applied entries from manual_coords.json supersede all earlier
    phases, allowing precise hand-placed anchors.

Phase 3 - OSRM linear interpolation (two passes)
    Between every consecutive pair of geocoded "anchor" stops within a
    route, fetch the road geometry via the OSRM Route API and place each
    intermediate stop proportionally along that path using the cumulative
    km_from_previous_station values.  Two passes let stops geocoded in the
    first pass act as anchors in the second.

Output:
    station_finder/stops_geocoded.json
        { canonical_name: {lat, lon, source, ...}, ... }

Usage:
    python station_finder/geocode_stations.py
"""

from __future__ import annotations

import sys
sys.stdout.reconfigure(encoding="utf-8")

import csv
import json
import math
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

import requests
from rapidfuzz import fuzz
from rapidfuzz import process as rfprocess

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LISTA_PATH = PROJECT_ROOT / "parsed" / "lista_statiilor.json"
GEOJSON_PATH = PROJECT_ROOT / "overpass" / "export.geojson"
CMAP_PATH = PROJECT_ROOT / "station_finder" / "canonical_map.json"
NOM_CACHE_PATH = PROJECT_ROOT / "station_finder" / "nominatim_cache.json"
MANUAL_COORDS_PATH = PROJECT_ROOT / "station_finder" / "manual_coords.json"
TRANSBUS_STOPS_PATH = PROJECT_ROOT / "transbus_stations" / "transbus_stops.txt"
OUT_PATH = PROJECT_ROOT / "station_finder" / "stops_geocoded.json"

# Bounding box for Buzău county (lon_min, lat_min, lon_max, lat_max)
BUZAU_BBOX = (26.30, 44.95, 27.30, 45.70)

# ---------------------------------------------------------------------------
# Text normalisation (mirrors build_station_set.py)
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Lowercase, strip diacritics, unify î↔â, collapse whitespace."""
    name = name.strip().rstrip("*").strip()
    name = name.replace("\u00ee", "\u00e2").replace("\u00ce", "\u00c2")
    name = "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", name).lower().strip()


# Words that are pure stop designations (not place names) that can be
# stripped to get a Nominatim-searchable locality name.
# NOTE: "autogara/autogară" is intentionally NOT here — it IS a real named
# amenity and must be kept so Nominatim can find it.
_STRIP_QUALIFIERS = re.compile(
    r"\b(centru|ramificatie|ramificaţie|scoala|şcoala|şcoală|"
    r"consiliul\s+local|bifurcatie|cap\s+traseu|cl\.?\s*local|"
    r"intersect\w*|piata|piaţă|brutarie|brut[aă]rie|combinat|"
    r"abator|electrica|magazin|primarie|primărie|dispensar|pod|parc|"
    r"terase?|uzina)\b",
    re.IGNORECASE | re.UNICODE,
)

_TRAILING_NUMBER = re.compile(r"\s+\d+$")


def _nominatim_query(name: str) -> str:
    """
    Derive a Nominatim search string from a station canonical name.
    Returns '' if the name is a pure numbered stop designation.

    Strategy:
    1. Skip names that end with a plain integer ("Boboc 1", "Murgesti 3").
    2. Try the FULL name first; Nominatim handles extra words well.
    3. If the full name is shorter than 3 chars after stripping generic
       qualifiers, fall back to that stripped form.
    """
    # Skip plain numbered stops: "Boboc 1", "Murgesti 3", etc.
    if _TRAILING_NUMBER.search(name):
        return ""

    # Use the full name as primary query — Nominatim handles extra context.
    # But also compute the stripped fallback for the cache key deduplication.
    stripped = _STRIP_QUALIFIERS.sub("", name).strip(" -,")
    stripped = re.sub(r"\s+", " ", stripped).strip()

    # If full name is itself a generic word (< 3 chars without qualifiers),
    # skip it entirely.
    if len(stripped) < 3:
        return ""

    return name  # use the full canonical name as the search string


# ---------------------------------------------------------------------------
# Phase 0 - Transbus GTFS matching
# ---------------------------------------------------------------------------

def load_transbus_stops(
    path: Path,
) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """
    Load transbus_stops.txt (GTFS stops.txt format) and return
    (stops_by_norm, norm_keys) for fuzzy matching.
    """
    stops_by_norm: dict[str, tuple[float, float]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["stop_name"].strip()
            if not name:
                continue
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
            key = _normalize(name)
            if key not in stops_by_norm:
                stops_by_norm[key] = (lat, lon)
    return stops_by_norm, sorted(stops_by_norm.keys())


def match_transbus(
    canonical_name: str,
    stops_by_norm: dict[str, tuple[float, float]],
    norm_keys: list[str],
    threshold: int = 85,
) -> Optional[tuple[float, float]]:
    """
    Return (lat, lon) if canonical_name has a confident Transbus match,
    else None.  Exact normalised match takes priority over fuzzy.
    """
    norm = _normalize(canonical_name)
    if norm in stops_by_norm:
        return stops_by_norm[norm]
    result = rfprocess.extractOne(norm, norm_keys, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= threshold:
        return stops_by_norm[result[0]]
    return None


# ---------------------------------------------------------------------------
# Phase 1 - OSM matching
# ---------------------------------------------------------------------------

def load_osm_stops(
    geojson_path: Path,
) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """
    Return (osm_by_norm, osm_norm_keys).

    osm_by_norm maps normalized name → (lat, lon) for every named OSM
    feature (first occurrence wins when names collide after normalization).
    osm_norm_keys is the sorted list of keys for rapidfuzz matching.
    """
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    osm_by_norm: dict[str, tuple[float, float]] = {}
    for feature in data["features"]:
        name = feature["properties"].get("name", "").strip()
        if not name:
            continue
        lon, lat = feature["geometry"]["coordinates"][:2]
        key = _normalize(name)
        if key not in osm_by_norm:
            osm_by_norm[key] = (lat, lon)
    return osm_by_norm, sorted(osm_by_norm.keys())


def match_osm(
    canonical_name: str,
    osm_by_norm: dict[str, tuple[float, float]],
    osm_norm_keys: list[str],
    threshold: int = 72,
) -> Optional[tuple[float, float]]:
    """
    Return (lat, lon) if canonical_name has a high-confidence OSM match,
    else None.  Exact normalised match takes priority over fuzzy.
    """
    norm = _normalize(canonical_name)
    if norm in osm_by_norm:
        return osm_by_norm[norm]
    result = rfprocess.extractOne(
        norm, osm_norm_keys, scorer=fuzz.token_sort_ratio
    )
    if result and result[1] >= threshold:
        return osm_by_norm[result[0]]
    return None


# ---------------------------------------------------------------------------
# Phase 2 - Nominatim cache lookup
# ---------------------------------------------------------------------------

def apply_nominatim_cache(
    canonical_names: list[str],
    geocoded: dict[str, dict],
    cache: dict[str, Optional[list[float]]],
) -> int:
    """
    Apply pre-computed Nominatim results from `cache` to `geocoded`.
    Only touches names not already in geocoded.
    Returns the number of new matches applied.
    """
    hits = 0
    for name in canonical_names:
        if name in geocoded:
            continue
        query = _nominatim_query(name)
        if not query:
            continue
        if query in cache and cache[query]:
            cached = cache[query]
            geocoded[name] = {
                "lat": cached[0],
                "lon": cached[1],
                "source": "nominatim",
                "query": query,
            }
            hits += 1
    return hits


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two WGS-84 points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_at_fraction(
    pts: list[tuple[float, float]], fraction: float
) -> tuple[float, float]:
    """
    Return the WGS-84 point at `fraction` (0-1) along the polyline `pts`
    (list of (lat, lon)).  Linear interpolation within each segment.
    """
    fraction = max(0.0, min(1.0, fraction))
    if fraction == 0.0:
        return pts[0]
    if fraction == 1.0:
        return pts[-1]

    seg_lengths = [
        haversine(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
        for i in range(len(pts) - 1)
    ]
    total = sum(seg_lengths)
    if total == 0:
        return pts[0]

    target = fraction * total
    cum = 0.0
    for i, seg_len in enumerate(seg_lengths):
        if cum + seg_len >= target:
            t = (target - cum) / seg_len if seg_len > 0 else 0.0
            lat = pts[i][0] + t * (pts[i + 1][0] - pts[i][0])
            lon = pts[i][1] + t * (pts[i + 1][1] - pts[i][1])
            return (lat, lon)
        cum += seg_len
    return pts[-1]


# ---------------------------------------------------------------------------
# Phase 3 - OSRM linear interpolation
# ---------------------------------------------------------------------------

OSRM_BASE = "http://router.project-osrm.org/route/v1/driving"
_osrm_cache: dict[tuple[float, float, float, float], Optional[list[tuple[float, float]]]] = {}


def call_osrm_route(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> Optional[list[tuple[float, float]]]:
    """
    Return road geometry as [(lat, lon), …] between two points via OSRM,
    or None on failure.  Results are memoised for the lifetime of the run.
    """
    # Round to ~1 m precision to maximise cache hits
    key = (round(lat1, 5), round(lon1, 5), round(lat2, 5), round(lon2, 5))
    if key in _osrm_cache:
        return _osrm_cache[key]

    url = f"{OSRM_BASE}/{lon1},{lat1};{lon2},{lat2}"
    params = {"overview": "full", "geometries": "geojson"}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                _osrm_cache[key] = None
                return None
            coords = data["routes"][0]["geometry"]["coordinates"]
            pts = [(c[1], c[0]) for c in coords]  # OSRM → [lon,lat]; flip to (lat,lon)
            _osrm_cache[key] = pts
            time.sleep(0.5)  # be polite to the demo server
            return pts
        except Exception as exc:
            print(f"    OSRM error (attempt {attempt + 1}/3): {exc}")
            time.sleep(2)

    _osrm_cache[key] = None
    return None


def geocode_route_via_osrm(
    route: dict,
    geocoded: dict[str, dict],
    cmap: dict[str, str],
) -> None:
    """
    Interpolate unmatched stops within one route using OSRM road geometry.
    Updates `geocoded` in-place.
    """
    stations = route.get("stations", [])
    if not stations:
        return

    route_num = route.get("route_number", "?")

    # Build ordered list: (canonical_name, cumulative_km_from_start)
    ordered: list[tuple[str, float]] = []
    cum_km = 0.0
    for st in stations:
        raw = st.get("station_name", "").strip()
        if not raw:
            continue
        canon = cmap.get(raw, raw)
        cum_km += st.get("kilometers_from_previous_station", 0.0)
        ordered.append((canon, cum_km))

    # Anchors = stops already geocoded
    anchors = [
        (i, name, ckm)
        for i, (name, ckm) in enumerate(ordered)
        if name in geocoded
    ]

    if len(anchors) < 2:
        return  # cannot interpolate without two fixed points

    for seg_idx in range(len(anchors) - 1):
        a_i, a_name, a_cum = anchors[seg_idx]
        b_i, b_name, b_cum = anchors[seg_idx + 1]

        # Stations in the gap that still need coordinates
        gap = [
            (name, ckm)
            for name, ckm in ordered[a_i + 1 : b_i]
            if name not in geocoded
        ]
        if not gap:
            continue

        a_coord = geocoded[a_name]
        b_coord = geocoded[b_name]
        pts = call_osrm_route(
            a_coord["lat"], a_coord["lon"], b_coord["lat"], b_coord["lon"]
        )

        if pts is None:
            print(
                f"  Route {route_num}: OSRM failed for "
                f"'{a_name}' → '{b_name}', skipping {len(gap)} stops"
            )
            continue

        segment_km = b_cum - a_cum
        if segment_km <= 0:
            # Zero-length segment: put everything at anchor A
            for name, _ in gap:
                geocoded[name] = {
                    "lat": a_coord["lat"],
                    "lon": a_coord["lon"],
                    "source": "osrm_interpolated",
                    "anchors": [a_name, b_name],
                    "route": route_num,
                }
            continue

        print(
            f"  Route {route_num}: {len(gap)} stops between "
            f"'{a_name}' and '{b_name}'"
        )
        for name, ckm in gap:
            fraction = (ckm - a_cum) / segment_km
            fraction = max(0.0, min(1.0, fraction))
            lat, lon = point_at_fraction(pts, fraction)
            geocoded[name] = {
                "lat": lat,
                "lon": lon,
                "source": "osrm_interpolated",
                "anchors": [a_name, b_name],
                "route": route_num,
            }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _all_canonicals(routes: list[dict], cmap: dict[str, str]) -> list[str]:
    """Return sorted deduplicated list of all canonical station names."""
    seen: set[str] = set()
    for route in routes:
        for st in route.get("stations", []):
            raw = st.get("station_name", "").strip()
            if raw:
                seen.add(cmap.get(raw, raw))
    return sorted(seen)


def main() -> None:
    print("=== Buzău GTFS Geocoder ===\n")

    # Load data
    print("Loading data …")
    routes = json.loads(LISTA_PATH.read_text(encoding="utf-8"))
    cmap = json.loads(CMAP_PATH.read_text(encoding="utf-8"))
    nom_cache: dict[str, Optional[list[float]]] = (
        json.loads(NOM_CACHE_PATH.read_text(encoding="utf-8"))
        if NOM_CACHE_PATH.exists()
        else {}
    )

    all_canonicals = _all_canonicals(routes, cmap)
    print(f"  {len(routes)} routes, {len(all_canonicals)} canonical station names\n")

    geocoded: dict[str, dict] = {}

    # -----------------------------------------------------------------------
    # Phase 0 - Transbus GTFS matching
    # -----------------------------------------------------------------------
    print("Phase 0 - Transbus GTFS matching …")
    if TRANSBUS_STOPS_PATH.exists():
        tb_by_norm, tb_norm_keys = load_transbus_stops(TRANSBUS_STOPS_PATH)
        print(f"  {len(tb_norm_keys)} Transbus stops loaded")
        for name in all_canonicals:
            match = match_transbus(name, tb_by_norm, tb_norm_keys)
            if match:
                geocoded[name] = {"lat": match[0], "lon": match[1], "source": "transbus"}
        print(f"  Transbus: {len(geocoded)} matches")
    else:
        print(f"  Skipped — {TRANSBUS_STOPS_PATH} not found")
    print()

    # -----------------------------------------------------------------------
    # Phase 1 - OSM matching
    # -----------------------------------------------------------------------
    print("Phase 1 - OSM fuzzy matching …")
    osm_by_norm, osm_norm_keys = load_osm_stops(GEOJSON_PATH)
    print(f"  {len(osm_norm_keys)} named OSM stops loaded")

    osm_new = 0
    for name in all_canonicals:
        if name in geocoded:
            continue
        match = match_osm(name, osm_by_norm, osm_norm_keys)
        if match:
            geocoded[name] = {"lat": match[0], "lon": match[1], "source": "osm"}
            osm_new += 1

    print(f"  OSM: {osm_new} new matches (total {len(geocoded)})\n")

    # -----------------------------------------------------------------------
    # Phase 2 - Nominatim cache lookup
    # -----------------------------------------------------------------------
    print("Phase 2 - Nominatim cache lookup …")
    nom_hits = apply_nominatim_cache(all_canonicals, geocoded, nom_cache)
    print(f"  Applied {nom_hits} Nominatim cache hits (total {len(geocoded)})")
    print(f"  (To refresh cache: python station_finder/nominatim/geocode_nominatim.py)\n")

    # -----------------------------------------------------------------------
    # Phase 2b - Manual coordinates (override/supplement before OSRM)
    # -----------------------------------------------------------------------
    if MANUAL_COORDS_PATH.exists():
        manual = json.loads(MANUAL_COORDS_PATH.read_text(encoding="utf-8"))
        added = overridden = 0
        for name, coord in manual.items():
            if name in all_canonicals:
                entry = {
                    "lat": coord["lat"],
                    "lon": coord["lon"],
                    "source": coord.get("source", "manual"),
                }
                if name not in geocoded:
                    added += 1
                else:
                    overridden += 1
                geocoded[name] = entry
        print(f"Phase 2b - Manual coords: {added} added, {overridden} overridden\n")

    # -----------------------------------------------------------------------
    # Phase 3 - OSRM interpolation (two passes)
    # -----------------------------------------------------------------------
    print("Phase 3 - OSRM road interpolation …")
    for pass_num in range(1, 3):
        before = len(geocoded)
        print(f"\n  Pass {pass_num}:")
        for route in routes:
            geocode_route_via_osrm(route, geocoded, cmap)
        gained = len(geocoded) - before
        print(f"  Pass {pass_num} complete: +{gained} new stops geocoded")
        if gained == 0:
            break  # converged

    # -----------------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------------
    by_source: dict[str, int] = {}
    for v in geocoded.values():
        src = v["source"]
        by_source[src] = by_source.get(src, 0) + 1

    total = len(all_canonicals)
    print(f"\n=== Results ===")
    print(f"Geocoded : {len(geocoded)} / {total} canonical stations")
    print(f"Missing  : {total - len(geocoded)}")
    for src, count in sorted(by_source.items()):
        print(f"  {src:30s} {count:4d}")

    if total - len(geocoded) > 0:
        missing = sorted(n for n in all_canonicals if n not in geocoded)
        print("\nUnmatched stations:")
        for n in missing:
            print(f"  {n}")

    OUT_PATH.write_text(
        json.dumps(geocoded, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote → {OUT_PATH}")


if __name__ == "__main__":
    main()
