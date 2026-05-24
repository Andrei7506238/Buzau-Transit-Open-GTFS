"""Quick validation of station_finder/stops_geocoded.json.

Usage:
    python build_pipeline/scripts/validate_geocoding.py
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent.parent
geo = json.loads((ROOT / "station_finder/stops_geocoded.json").read_text(encoding="utf-8"))
routes = json.loads((ROOT / "parsed/lista_statiilor.json").read_text(encoding="utf-8"))
cmap = json.loads((ROOT / "station_finder/canonical_map.json").read_text(encoding="utf-8"))

print("=== Coverage per route ===")
incomplete = []
for route in routes:
    num = route["route_number"]
    stations = route.get("stations", [])
    total = len(stations)
    geocoded_count = sum(
        1 for s in stations
        if cmap.get(s.get("station_name", ""), s.get("station_name", "")) in geo
    )
    pct = geocoded_count / total * 100 if total else 0
    flag = "" if pct == 100 else f"  <- {total - geocoded_count} missing"
    print(f"  Route {num}: {geocoded_count}/{total} ({pct:.0f}%){flag}")
    if pct < 100:
        incomplete.append(num)

print()
print(f"Routes with full coverage : {len(routes) - len(incomplete)}/{len(routes)}")
print(f"Routes with gaps          : {len(incomplete)} ({', '.join(incomplete)})")

# Bounding-box sanity check
LAT_MIN, LAT_MAX = 44.95, 45.72
LON_MIN, LON_MAX = 26.30, 27.30

outliers = [
    (name, v)
    for name, v in geo.items()
    if not (LAT_MIN <= v["lat"] <= LAT_MAX and LON_MIN <= v["lon"] <= LON_MAX)
]
print()
print(f"=== Bounding-box outliers (outside Buzau county bbox): {len(outliers)} ===")
for name, v in outliers:
    src = v["source"]
    print(f"  {name}: lat={v['lat']:.5f} lon={v['lon']:.5f}  [{src}]")

print()
print(f"Total geocoded: {len(geo)} stops")
by_src = {}
for v in geo.values():
    by_src[v["source"]] = by_src.get(v["source"], 0) + 1
for src, cnt in sorted(by_src.items()):
    print(f"  {src:30s} {cnt:4d}")

# Inspect worst-coverage routes
print()
print("=== Low-coverage route details ===")
WORST = {"058", "059", "021", "022", "047", "053", "055"}
for route in routes:
    num = route["route_number"]
    if num not in WORST:
        continue
    total = len(route.get("stations", []))
    hit = sum(1 for s in route.get("stations", [])
              if cmap.get(s.get("station_name", ""), s.get("station_name", "")) in geo)
    print(f"\nRoute {num} ({hit}/{total}): {route['from_location']} -> {route['to_location']}")
    for s in route.get("stations", []):
        nm = cmap.get(s.get("station_name", ""), s.get("station_name", ""))
        tag = "OK " if nm in geo else "---"
        print(f"  [{tag}] {nm}")
