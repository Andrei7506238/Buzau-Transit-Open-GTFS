"""
overpass/fetch_overpass.py
==========================
Fetch the Buzău bus-stop export from the Overpass API and write
overpass/export.geojson in the same GeoJSON format produced by Overpass Turbo.

Usage:
    python overpass/fetch_overpass.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
QUERY_PATH = ROOT / "overpass" / "request.txt"
OUT_PATH = ROOT / "overpass" / "export.geojson"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Overpass ToS requires a descriptive User-Agent
HEADERS = {"User-Agent": "buzau-transit-gtfs/1.0 (https://github.com)"}


def overpass_to_geojson(data: dict) -> dict:
    """Convert Overpass JSON response to a GeoJSON FeatureCollection."""
    features = []
    for el in data.get("elements", []):
        el_type = el.get("type", "")
        tags = el.get("tags", {})

        if el_type == "node":
            lat = el.get("lat")
            lon = el.get("lon")
        else:
            # ways/relations: use the center computed by 'out center'
            center = el.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")

        if lat is None or lon is None:
            continue

        el_id = f"{el_type}/{el['id']}"
        props = {"@id": el_id}
        props.update(tags)

        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "id": el_id,
        })

    return {
        "type": "FeatureCollection",
        "generator": "overpass-api",
        "copyright": (
            "The data included in this document is from www.openstreetmap.org. "
            "The data is made available under ODbL."
        ),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "features": features,
    }


def main() -> None:
    query = QUERY_PATH.read_text(encoding="utf-8")

    print("Fetching Overpass export \u2026")
    print(f"  query : {QUERY_PATH.relative_to(ROOT)}")

    resp = None
    for url in OVERPASS_ENDPOINTS:
        print(f"  trying: {url}")
        try:
            resp = requests.get(
                url, params={"data": query}, headers=HEADERS, timeout=240
            )
            resp.raise_for_status()
            break
        except requests.HTTPError as exc:
            print(f"  failed ({exc}), trying next endpoint …")
            resp = None

    if resp is None:
        print("ERROR: all Overpass endpoints failed.", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    n_elements = len(data.get("elements", []))
    print(f"  received: {n_elements} elements")

    geojson = overpass_to_geojson(data)
    n_features = len(geojson["features"])
    n_skipped = n_elements - n_features
    print(f"  features : {n_features} (skipped {n_skipped} without coordinates)")

    OUT_PATH.write_text(
        json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote → {OUT_PATH}")
    print("Next: re-run station_finder/geocode_stations.py to pick up new OSM data.")


if __name__ == "__main__":
    main()
