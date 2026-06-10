"""
station_finder/nominatim/geocode_nominatim.py
=============================================
Standalone Nominatim geocoding script for Buzău county bus stops.

Queries the Nominatim API for every canonical station name not yet in
the cache and persists results to station_finder/nominatim_cache.json.

Run this script BEFORE geocode_stations.py to populate or refresh the
Nominatim cache.  A full run with an empty cache takes ~9 minutes
(≤ 1 request/second, per the Nominatim ToS).  Subsequent runs are
instant because every query is cached.

Usage:
    python station_finder/nominatim/geocode_nominatim.py
    python station_finder/nominatim/geocode_nominatim.py --force
        Force re-querying entries that previously returned null (no result).
"""

from __future__ import annotations

import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LISTA_PATH = PROJECT_ROOT / "parsed" / "lista_statiilor.json"
CMAP_PATH = PROJECT_ROOT / "station_finder" / "canonical_map.json"
NOM_CACHE_PATH = PROJECT_ROOT / "station_finder" / "nominatim_cache.json"

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
    Returns '' if the name is a plain numbered stop or too generic.
    """
    if _TRAILING_NUMBER.search(name):
        return ""
    stripped = _STRIP_QUALIFIERS.sub("", name).strip(" -,")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if len(stripped) < 3:
        return ""
    return name  # use full canonical name for best results


# ---------------------------------------------------------------------------
# Nominatim API
# ---------------------------------------------------------------------------

_NOM_URL = "https://nominatim.openstreetmap.org/search"
_NOM_HEADERS = {"User-Agent": "buzau-gtfs-builder/1.0 (local research project)"}


def _in_bbox(lat: float, lon: float) -> bool:
    lon_min, lat_min, lon_max, lat_max = BUZAU_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def nominatim_geocode(query: str) -> Optional[tuple[float, float]]:
    """
    Query Nominatim for `query` restricted to the Buzău county bounding box.
    Returns (lat, lon) or None.  Does NOT rate-limit; caller must sleep.
    """
    lon_min, lat_min, lon_max, lat_max = BUZAU_BBOX
    params = {
        "q": f"{query} Buzău România",
        "format": "json",
        "limit": 5,
        "viewbox": f"{lon_min},{lat_max},{lon_max},{lat_min}",
        "bounded": 1,
    }
    try:
        resp = requests.get(_NOM_URL, params=params, headers=_NOM_HEADERS, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        for r in results:
            lat, lon = float(r["lat"]), float(r["lon"])
            if _in_bbox(lat, lon):
                return (lat, lon)
    except Exception as exc:
        print(f"  Nominatim error for {query!r}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(force: bool = False) -> None:
    print("=== Buzău GTFS - Nominatim Geocoder ===\n")

    routes = json.loads(LISTA_PATH.read_text(encoding="utf-8"))
    cmap = json.loads(CMAP_PATH.read_text(encoding="utf-8"))
    cache: dict[str, Optional[list[float]]] = (
        json.loads(NOM_CACHE_PATH.read_text(encoding="utf-8"))
        if NOM_CACHE_PATH.exists()
        else {}
    )

    all_canonicals = _all_canonicals(routes, cmap)
    print(f"Canonical stations : {len(all_canonicals)}")
    print(f"Cache size         : {len(cache)} entries\n")

    # Determine which names need querying
    to_query: list[tuple[str, str]] = []  # (canonical_name, query_string)
    for name in all_canonicals:
        query = _nominatim_query(name)
        if not query:
            continue  # skip numbered/generic stops
        if query in cache and not (force and cache[query] is None):
            continue  # already cached (or null and not forcing)
        to_query.append((name, query))

    print(f"Queries to make    : {len(to_query)}")
    if not to_query:
        print("Nothing to do - cache is up to date.")
        return

    hits = 0
    for i, (name, query) in enumerate(to_query, 1):
        time.sleep(1.1)  # Nominatim ToS: ≤ 1 req/s
        result = nominatim_geocode(query)
        cache[query] = [result[0], result[1]] if result else None
        status = f"OK  ({result[0]:.5f}, {result[1]:.5f})" if result else "null"
        print(f"  [{i:3d}/{len(to_query)}] {name!r:50s}  {status}")
        if result:
            hits += 1

    print(f"\nNew hits  : {hits}")
    print(f"Cache size: {len(cache)} entries")

    NOM_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved → {NOM_CACHE_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Populate the Nominatim geocoding cache for Buzău bus stops."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-query entries that previously returned null (no result).",
    )
    args = parser.parse_args()
    main(force=args.force)
