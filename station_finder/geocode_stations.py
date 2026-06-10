"""
station_finder/geocode_stations.py
===================================
Four-phase geocoding for Buzău county bus stops.

Phase 1 - Combined source scoring
    For every canonical station name, gather candidates from all three
    sources simultaneously:

      • Transbus GTFS  (transbus_stations/transbus_stops.txt, mdb-2106)
      • OSM named nodes  (overpass/export.geojson via rapidfuzz)
      • Nominatim cache  (nominatim_cache.json)

    Each candidate is scored as  source_weight × fuzzy_score (0-100).
    The candidate with the highest final score wins.

    Default weights:  Transbus=1.00, OSM=0.90, Nominatim=0.75
    Minimum fuzzy scores before a candidate is considered:
        Transbus=90, OSM=72; Nominatim cache hits are always score 100.

    To refresh or extend the Nominatim cache, run:
        python station_finder/nominatim/geocode_nominatim.py

Phase 2 - Manual coordinates (override)
    Always-applied entries from manual_coords.json supersede all earlier
    phases, allowing precise hand-placed anchors.

Phase 3 - Route-context outlier repair
    For every route, inspect each geocoded stop against its nearest
    geocoded neighbors using timetable km as ground truth.  Two sub-checks:

    Interior stops (geocoded anchor on BOTH sides):
      Since haversine ≤ road distance always, haversine(P,X)/timetable_km(P→X)
      or haversine(X,N)/timetable_km(X→N) exceeding 1.0 is physically
      impossible.  Flagged when either ratio > REPAIR_INTERIOR_RATIO on a
      leg ≥ REPAIR_INTERIOR_MIN_KM.  Evicted so Phase 4 re-interpolates
      from clean anchors.

    Terminal stops (geocoded anchor on ONE side only):
      Same haversine/timetable_km ratio check against the single anchor.
      Flagged when ratio > REPAIR_TERMINAL_RATIO and
      haversine > REPAIR_TERMINAL_MIN_KM.  Permanently removed.

    Each pass evicts the single worst outlier per route.  Correctly-placed
    stops that only appear bad due to a wrong neighbour (shadow outliers)
    have lower ratios, so the wrong neighbour is removed first.

Phase 4 - OSRM linear interpolation (two passes)
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

# Phase 3 thresholds
REPAIR_INTERIOR_RATIO   = 5.0   # flag interior stop when haversine / timetable_km > this on either leg
REPAIR_INTERIOR_MIN_KM  = 1.0   # skip ratio check on legs shorter than this (avoids timetable-rounding noise)
REPAIR_TERMINAL_RATIO   = 1.5   # flag terminal stop when haversine / timetable_km > this
REPAIR_TERMINAL_MIN_KM  = 5.0   # minimum haversine km before a terminal is flagged

# Source weights for combined Phase 0/1/2 scoring  (final_score = weight × fuzzy_score)
TRANSBUS_WEIGHT  = 1.00   # highest-quality: city-bus GTFS stops
OSM_WEIGHT       = 0.90   # OpenStreetMap named nodes
NOMINATIM_WEIGHT = 0.75   # Nominatim geocoder cache

# Minimum fuzzy score (0-100) a candidate must reach before being considered
TRANSBUS_MIN_SCORE = 90
OSM_MIN_SCORE      = 72

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
# NOTE: "autogara/autogară" is intentionally NOT here  - it IS a real named
# amenity and must eb kept so Nominatim can find it.
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

    # Use the full name as primary query  - Nominatim handles extra context.
    # But also compute the stripped fallback for the cache key deduplication.
    stripped = _STRIP_QUALIFIERS.sub("", name).strip(" -,")
    stripped = re.sub(r"\s+", " ", stripped).strip()

    # If full name is itself a generic word (< 3 chars without qualifiers),
    # skip it entirely.
    if len(stripped) < 3:
        return ""

    return name  # use the full canonical name as the search string


# ---------------------------------------------------------------------------
# Phase 1 sources - Transbus GTFS
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


def candidates_transbus(
    canonical_name: str,
    stops_by_norm: dict[str, tuple[float, float]],
    norm_keys: list[str],
    min_score: int = TRANSBUS_MIN_SCORE,
) -> list[tuple[float, float, float]]:
    """
    Return [(lat, lon, fuzzy_score)] candidates from the Transbus GTFS.
    Exact normalised matches receive score 100.  Returns [] when
    stops_by_norm is empty or no candidate reaches min_score.

    Qualifier words are stripped from the query before fuzzy scoring to
    prevent generic suffixes from driving false matches.
    """
    if not stops_by_norm:
        return []
    norm = _normalize(canonical_name)
    if norm in stops_by_norm:
        lat, lon = stops_by_norm[norm]
        return [(lat, lon, 100.0)]
    norm_stripped = _STRIP_QUALIFIERS.sub("", norm).strip(" -,")
    norm_stripped = re.sub(r"\s+", " ", norm_stripped).strip()
    if len(norm_stripped) < 3:
        return []
    result = rfprocess.extractOne(norm_stripped, norm_keys, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= min_score:
        lat, lon = stops_by_norm[result[0]]
        return [(lat, lon, float(result[1]))]
    return []


# ---------------------------------------------------------------------------
# Phase 1 sources - OSM
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
        props = feature["properties"]
        lon, lat = feature["geometry"]["coordinates"][:2]
        # Index all name variants so fuzzy matching can reach alt/localised names.
        # OSM bus stops may carry name:ro (Romanian-script form) and alt_name
        # in addition to the primary name tag.
        for field in ("name", "alt_name", "name:ro"):
            raw = props.get(field, "").strip()
            if not raw:
                continue
            key = _normalize(raw)
            if key not in osm_by_norm:
                osm_by_norm[key] = (lat, lon)
    return osm_by_norm, sorted(osm_by_norm.keys())


def candidates_osm(
    canonical_name: str,
    osm_by_norm: dict[str, tuple[float, float]],
    osm_norm_keys: list[str],
    min_score: int = OSM_MIN_SCORE,
) -> list[tuple[float, float, float]]:
    """
    Return [(lat, lon, fuzzy_score)] candidates from OSM named nodes.
    Exact normalised matches receive score 100.

    Qualifier words (ramificatie, centru, scoala, …) are stripped from
    the query before fuzzy scoring so that a shared generic suffix cannot
    dominate the similarity score and produce false matches
    (e.g. "Berca ramificatie" must NOT match "Dara Ramificatie").
    If the stripped query is shorter than 3 chars, fuzzy matching is
    skipped entirely.
    """
    norm = _normalize(canonical_name)
    if norm in osm_by_norm:
        lat, lon = osm_by_norm[norm]
        return [(lat, lon, 100.0)]
    norm_stripped = _STRIP_QUALIFIERS.sub("", norm).strip(" -,")
    norm_stripped = re.sub(r"\s+", " ", norm_stripped).strip()
    if len(norm_stripped) < 3:
        return []
    result = rfprocess.extractOne(
        norm_stripped, osm_norm_keys, scorer=fuzz.token_sort_ratio
    )
    if result and result[1] >= min_score:
        lat, lon = osm_by_norm[result[0]]
        return [(lat, lon, float(result[1]))]
    return []


# ---------------------------------------------------------------------------
# Phase 1 sources - Nominatim cache
# ---------------------------------------------------------------------------

def candidates_nominatim(
    canonical_name: str,
    cache: dict[str, Optional[list[float]]],
) -> list[tuple[float, float, float]]:
    """
    Return [(lat, lon, 100.0)] when the canonical name has a Nominatim
    cache entry, else [].  The score is always 100 because the cache
    already contains a confirmed geocoder result.
    """
    query = _nominatim_query(canonical_name)
    if not query:
        return []
    if query in cache and cache[query]:
        cached = cache[query]
        return [(cached[0], cached[1], 100.0)]
    return []


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
# Phase 4 - OSRM interpolation
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
# Phase 3 - Route-context outlier repair
# ---------------------------------------------------------------------------

def repair_route_outliers(
    routes: list[dict],
    geocoded: dict[str, dict],
    cmap: dict[str, str],
    interior_ratio: float = REPAIR_INTERIOR_RATIO,
    interior_min_km: float = REPAIR_INTERIOR_MIN_KM,
    terminal_ratio: float = REPAIR_TERMINAL_RATIO,
    terminal_min_km: float = REPAIR_TERMINAL_MIN_KM,
) -> tuple[int, set[str]]:
    """
    Detect and evict stops whose geocoded coordinates are implausible
    given their in-route neighbors and timetable km ground-truth.

    Since haversine ≤ road distance always, haversine / timetable_km > 1.0
    is physically impossible.  Both checks exploit this invariant.

    Runs before Phase 4 interpolation so that every anchor used in the
    ratio check is a real geocoded position (Transbus / OSM / Nominatim /
    manual)  - no poisoning from wrong interpolated stops.  Flagged stops
    are evicted; Phase 4 fills the gaps from the now-clean anchor set.

    Interior stops (geocoded anchor on BOTH sides):
      Compute haversine(P,X)/timetable_km(P→X) and haversine(X,N)/timetable_km(X→N).
      Legs shorter than interior_min_km are skipped (avoids noise on tiny segments).
      Flagged when max(ratio_px, ratio_xn) > interior_ratio.
      → Evicted so Phase 4 re-interpolates from clean anchors.

    Terminal stops (geocoded anchor on ONE side only):
      Same ratio check against the single known anchor.
      Flagged when ratio > terminal_ratio AND haversine > terminal_min_km.
      → Evicted; caller adds to terminal_blacklist so Phase 4 never re-adds them.

    First-route-wins when a stop is flagged by multiple routes.
    Per-route eviction: at the end of each pass the single worst outlier
    in every route is evicted.  Shadow stops (correct stops that only look
    bad because a neighbour is wrong) have a lower ratio and are deferred
    until the neighbour is removed.
    Returns (n_interior_evicted, terminal_evicted_set).
    """
    # (worst_ratio, name, "interior"|"terminal", route_num)  - populated during route scan
    detected: list[tuple[float, str, str, str]] = []
    repairs: dict[str, dict] = {}   # interior outliers (for first-route-wins guard)
    removals: dict[str, str] = {}   # terminal outliers (for first-route-wins guard)

    for route in routes:
        stations = route.get("stations", [])
        if len(stations) < 3:
            continue
        route_num = route.get("route_number", "?")

        ordered: list[tuple[str, float]] = []
        cum_km = 0.0
        for st in stations:
            raw = st.get("station_name", "").strip()
            if not raw:
                continue
            canon = cmap.get(raw, raw)
            cum_km += st.get("kilometers_from_previous_station", 0.0)
            ordered.append((canon, cum_km))

        for i, (name, ckm) in enumerate(ordered):
            if name not in geocoded:
                continue
            if name in repairs or name in removals:
                continue  # already scheduled; first-route-wins

            prev = next(
                (j for j in range(i - 1, -1, -1) if ordered[j][0] in geocoded),
                None,
            )
            nxt = next(
                (j for j in range(i + 1, len(ordered)) if ordered[j][0] in geocoded),
                None,
            )

            x_c = geocoded[name]

            if prev is not None and nxt is not None:
                # --- interior timetable-ratio check ---
                # haversine ≤ road distance always, so haversine/timetable_km > 1
                # is physically impossible; flag when either leg exceeds interior_ratio.
                p_name, p_ckm = ordered[prev]
                n_name, n_ckm = ordered[nxt]

                # Skip degenerate case: same stop on both sides (dead-end loop)
                if p_name == n_name:
                    continue

                # Never replace manual or GPS-surveyed (transbus) stops
                if x_c["source"] in ("manual", "transbus"):
                    continue

                p_c = geocoded[p_name]
                n_c = geocoded[n_name]

                timetable_px = ckm - p_ckm        # road km P→X per timetable
                timetable_xn = n_ckm - ckm        # road km X→N per timetable
                hav_px = haversine(p_c["lat"], p_c["lon"], x_c["lat"], x_c["lon"]) / 1000.0
                hav_xn = haversine(x_c["lat"], x_c["lon"], n_c["lat"], n_c["lon"]) / 1000.0

                ratio_px = hav_px / timetable_px if timetable_px >= interior_min_km else 0.0
                ratio_xn = hav_xn / timetable_xn if timetable_xn >= interior_min_km else 0.0
                worst_ratio = max(ratio_px, ratio_xn)

                if worst_ratio > interior_ratio:
                    repairs[name] = {"route": route_num, "old_source": x_c["source"]}
                    detected.append((worst_ratio, name, "interior", route_num))
                    print(
                        f"  Route {route_num}: '{name}' interior outlier "
                        f"(hav/timetable={worst_ratio:.2f} > {interior_ratio}, "
                        f"hav={hav_px:.1f}+{hav_xn:.1f} km, "
                        f"timetable={timetable_px:.1f}+{timetable_xn:.1f} km, "
                        f"was {x_c['source']})"
                    )

            else:
                # --- terminal haversine/timetable-km ratio check ---
                if prev is not None and nxt is None:
                    anchor_name, anchor_ckm = ordered[prev]
                    road_km = ckm - anchor_ckm
                    direction = "trailing"
                elif nxt is not None and prev is None:
                    anchor_name, anchor_ckm = ordered[nxt]
                    road_km = anchor_ckm - ckm
                    direction = "leading"
                else:
                    continue  # no anchors at all  - skip

                if road_km <= 0:
                    continue

                a_c = geocoded[anchor_name]
                hav_km = haversine(a_c["lat"], a_c["lon"], x_c["lat"], x_c["lon"]) / 1000.0
                ratio = hav_km / road_km

                if ratio > terminal_ratio and hav_km > terminal_min_km:
                    src = x_c["source"]
                    if src == "manual":
                        print(
                            f"  Route {route_num}: '{name}' terminal outlier skipped "
                            f"(haversine={hav_km:.1f} km, timetable={road_km:.1f} km, "
                            f"ratio={ratio:.2f}, source=manual  - authoritative, not removed)"
                        )
                    else:
                        removals[name] = route_num
                        detected.append((ratio, name, "terminal", route_num))
                        print(
                            f"  Route {route_num}: '{name}' {direction} terminal outlier "
                            f"(haversine={hav_km:.1f} km, timetable={road_km:.1f} km, "
                            f"ratio={ratio:.2f} > {terminal_ratio}, was {src})"
                        )

    # For each route, evict only its single worst outlier per pass.
    # Shadowing is prevented naturally: a correctly-placed C that only looks
    # bad because neighbour B is wrong has a lower ratio than B.  B is evicted
    # first; next pass C's anchor is clean and it is no longer flagged.
    best_per_route: dict[str, tuple[float, str, str, str]] = {}
    for entry in detected:
        ratio, name, kind, rnum = entry
        if rnum not in best_per_route or ratio > best_per_route[rnum][0]:
            best_per_route[rnum] = entry
    to_evict = list(best_per_route.values())

    skipped = len(detected) - len(to_evict)
    if skipped:
        print(
            f"  (detected {len(detected)} outliers across {len(best_per_route)} routes; "
            f"evicting 1 worst per route this pass  - {skipped} deferred to next pass)"
        )

    evicted_interior = 0
    evicted_terminal: set[str] = set()
    for _, name, kind, _rnum in to_evict:
        geocoded.pop(name, None)
        if kind == "interior":
            evicted_interior += 1
        else:
            evicted_terminal.add(name)

    return evicted_interior, evicted_terminal


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
    # Phase 1 - Combined source scoring
    #   For each station: collect candidates from Transbus, OSM, and
    #   Nominatim; score each as  weight × fuzzy_score (0-100); pick best.
    # -----------------------------------------------------------------------
    print("Phase 1 - Combined source scoring (Transbus + OSM + Nominatim) …")
    osm_by_norm, osm_norm_keys = load_osm_stops(GEOJSON_PATH)
    print(f"  {len(osm_norm_keys)} named OSM stops loaded")

    tb_by_norm: dict[str, tuple[float, float]] = {}
    tb_norm_keys: list[str] = []
    if TRANSBUS_STOPS_PATH.exists():
        tb_by_norm, tb_norm_keys = load_transbus_stops(TRANSBUS_STOPS_PATH)
        print(f"  {len(tb_norm_keys)} Transbus stops loaded")
    else:
        print(f"  Transbus skipped  - {TRANSBUS_STOPS_PATH} not found")

    for name in all_canonicals:
        all_candidates: list[tuple[float, float, float, str]] = []

        for lat, lon, score in candidates_transbus(name, tb_by_norm, tb_norm_keys):
            all_candidates.append((lat, lon, TRANSBUS_WEIGHT * score, "transbus"))
        for lat, lon, score in candidates_osm(name, osm_by_norm, osm_norm_keys):
            all_candidates.append((lat, lon, OSM_WEIGHT * score, "osm"))
        for lat, lon, score in candidates_nominatim(name, nom_cache):
            all_candidates.append((lat, lon, NOMINATIM_WEIGHT * score, "nominatim"))

        if all_candidates:
            best_lat, best_lon, best_score, best_src = max(all_candidates, key=lambda c: c[2])
            geocoded[name] = {
                "lat": best_lat,
                "lon": best_lon,
                "source": best_src,
                "match_score": round(best_score, 1),
            }

    by_src: dict[str, int] = {}
    for v in geocoded.values():
        src = v["source"]
        by_src[src] = by_src.get(src, 0) + 1
    src_summary = ", ".join(f"{s}={c}" for s, c in sorted(by_src.items()))
    print(f"  {len(geocoded)} matched ({src_summary})")
    print(f"  (To refresh Nominatim cache: python station_finder/nominatim/geocode_nominatim.py)\n")

    # -----------------------------------------------------------------------
    # Phase 2 - Manual coordinates (override/supplement before outlier check)
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
        print(f"Phase 2 - Manual coords: {added} added, {overridden} overridden\n")

    # -----------------------------------------------------------------------
    # Phase 3 - Outlier eviction (pre-interpolation)
    # -----------------------------------------------------------------------
    # Runs before Phase 4 so every anchor used in the ratio check is a real
    # geocoded position (Transbus / OSM / Nominatim / manual).  No OSRM stops
    # exist yet, so there is no risk of a wrong interpolated point poisoning
    # the check for its neighbors.  Evicted stops are gaps that Phase 4 fills
    # correctly from the now-clean anchor set.
    # Iterative top-K eviction: each pass removes only the K worst outliers
    # (by haversine/timetable ratio) so that shadow-flagged stops get a clean
    # recheck once their bad neighbour is gone.  No OSRM calls happen here.
    terminal_blacklist: set[str] = set()
    phase3_pass = 0
    while True:
        phase3_pass += 1
        label = "Phase 3" if phase3_pass == 1 else f"Phase 3 (pass {phase3_pass})"
        print(f"{label} - Outlier eviction …")
        n_interior, removed_names = repair_route_outliers(routes, geocoded, cmap)
        terminal_blacklist.update(removed_names)
        n_terminal = len(removed_names)
        if n_interior or n_terminal:
            print(f"  Evicted {n_interior} interior, {n_terminal} terminal")
        else:
            print("  Stable  - no outliers detected")
        print()
        if not n_interior and not n_terminal:
            break

    # -----------------------------------------------------------------------
    # Phase 4 - OSRM interpolation (using only validated anchors)
    # -----------------------------------------------------------------------
    print("Phase 4 - OSRM road interpolation …")
    for pass_num in range(1, 3):
        before = len(geocoded)
        print(f"\n  Pass {pass_num}:")
        for route in routes:
            geocode_route_via_osrm(route, geocoded, cmap)
        # Prevent blacklisted terminals from being re-added
        for name in terminal_blacklist:
            geocoded.pop(name, None)
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
