"""
build_pipeline/scripts/make_geojson_preview.py
==============================================
Generate a GeoJSON preview of all geocoded stops for geojson.io visual review.

Output: build_pipeline/output/stops_preview.geojson

Usage:
    python build_pipeline/scripts/make_geojson_preview.py
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent

geo = json.loads((ROOT / "station_finder" / "stops_geocoded.json").read_text(encoding="utf-8"))

# Known stops that trigger fast-travel warnings
fast_stops = {"Gura Teghii", "Tainița"}

SOURCE_COLORS = {
    "transbus":          "#ff0000",  # red   - Transbus GTFS
    "osm":               "#00aa00",  # green - OSM node
    "nominatim":         "#0066ff",  # blue  - Nominatim API
    "manual":            "#ff8800",  # orange - manual coords
    "osrm_interpolated": "#aa00aa",  # purple - OSRM road interpolation
}

features = []
for name, v in sorted(geo.items()):
    lat, lon = v["lat"], v["lon"]
    src = v.get("source", "unknown")
    color = SOURCE_COLORS.get(src, "#888888")
    flag = "⚠️ speed-warning" if name in fast_stops else ""
    features.append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "name": name,
            "source": src,
            "marker-color": color,
            "marker-size": "small",
            "note": flag,
        },
    })

geojson = {"type": "FeatureCollection", "features": features}
out = ROOT / "build_pipeline" / "output" / "stops_preview.geojson"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {len(features)} stops → {out}")
print("Open https://geojson.io and drag the file in to inspect.")
