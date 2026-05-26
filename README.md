# Buzau-Transit-Open-GTFS

## **EN** - Converts Buzău County (Romania) intercity bus timetables into a
[GTFS](https://gtfs.org/) feed.  
All data is stored directly in this repository and is based on the official
[Consiliul Județean Buzău](https://www.cjbuzau.ro) PDF schedules. The information is curated and then processed by an automated pipeline.
This pipeline extracts routes and schedules, geocodes every bus stop using a
five-phase process (Transbus GTFS → OpenStreetMap → Nominatim → OSRM interpolation → outlier repair), and finally generates a fully compliant, validator-passing GTFS package.

## **RO** - Transformă orarele de transport județean din Buzău într-un format
[GTFS](https://gtfs.org/) standardizat.  
Datele utilizate sunt stocate exclusiv în acest repository și se bazează pe orarele în format PDF publicate de [Consiliul Județean Buzău](https://www.cjbuzau.ro).
Informațiile sunt centralizate și verificate, după care un pipeline automat extrage rutele și orarele, obține coordonatele fiecărei stații printr-un proces în cinci etape (Transbus GTFS → OpenStreetMap → Nominatim →
interpolare OSRM → reparare outlieri) și generează un pachet GTFS complet, validat fără erori.

---

## Public beta status

This feed is currently published as **Public Beta / Preview**.

**Known issues**:
- intermediate stop times are intentionally left blank (only timetable anchor stops carry explicit times), so some journey planners may infer in-between timing differently.
- stop coordinates are based on OSM nodes and Nominatim results, which may be imprecise for some stops (especially in rural areas), manual corrections are applied where possible, but some errors may remain.

---

## Author

This project is developed and maintained by [Popa Andrei-Robert](https://andrei7506238.github.io/).

Contributions, suggestions, and feedback are welcomed, but the best way to support the project is by adding the missing coordinates for the stops on OpenStreetMap, which benefits everyone and improves the feed quality for all users.

If the exact timetable is available in a machine-readable format from the source authority, please let me know so I can update the feed with more accurate schedules and reduce the reliance on interpolation.

---

## Data sources

| Source | Description |
|---|---|
| [Consiliul Județean Buzău](https://www.cjbuzau.ro) | Source authority for the timetable PDFs |
| [OpenStreetMap](https://www.openstreetmap.org/) | Named bus stop nodes (Overpass export) |
| [Nominatim](https://nominatim.org/) | Geocoding API for place names |
| [OSRM](http://router.project-osrm.org/) | Road routing for stop interpolation |
| [Transbus mdb-2106](https://mobilitydatabase.org/feeds/gtfs/mdb-2106) | Buzău city bus GTFS feed (stop coordinates) |

---

## Repository layout

```
raw/                          # Source PDFs (not versioned - see below)
  Lista-statiilor.pdf         # Station lists per route
  Program-transport.pdf       # Timetables

parsed/                       # Intermediate JSON - generated from PDFs
  lista_statiilor.json
  program_transport.json

extractors/                   # PDF → JSON parsers
  scripts/
    schedule_parser.py        # Parses Lista-statiilor.pdf  → lista_statiilor.json
    route_parser.py           # Parses Program-transport.pdf → program_transport.json
  corrections.py              # Manual text-level corrections for OCR/parser artefacts

build_pipeline/               # Build/validation/preview scripts + generated outputs
  scripts/
    build_gtfs.py             # JSON + geocoded stops → output/gtfs/
    validate_geocoding.py     # Coverage report per route
    make_geojson_preview.py   # Writes output/stops_preview.geojson (stops + route paths)
  output/
    gtfs/                     # Generated GTFS feed
    stops_preview.geojson     # Generated GeoJSON preview (stops + route paths)

transbus_stations/            # Buzău city bus GTFS stops (source: mdb-2106)
  transbus_stops.txt          # GTFS stops.txt - used in Phase 0 geocoding

station_finder/               # Station geocoding
  geocode_stations.py         # 4-phase geocoder → stops_geocoded.json
  build_station_set.py        # Deduplicates names → stations.json + canonical_map.json
  nominatim/
    geocode_nominatim.py      # Standalone script - queries Nominatim, builds cache
  manual_coords.json          # ONLY file with hardcoded data (anchor coordinates)
  stations.json               # Generated - sorted canonical stop names
  canonical_map.json          # Generated - variant → canonical mapping
  nominatim_cache.json        # Generated - Nominatim API response cache
  stops_geocoded.json         # Generated - final geocoded coordinates

overpass/
  fetch_overpass.py           # Fetches OSM data from the Overpass API → export.geojson
  export.geojson              # OSM bus stop nodes (Overpass export)
  request.txt                 # The Overpass QL query used to generate export.geojson

build_pipeline/output/gtfs/   # Generated GTFS feed
  agency.txt, stops.txt, routes.txt, trips.txt,
  stop_times.txt, calendar.txt, feed_info.txt
```

---

## Geocoding pipeline

The geocoder resolves canonical stop names to coordinates in priority order across six phases.
Each phase only fills in stops not yet matched by an earlier phase.

| Phase | Source | Notes |
|---|---|---|
| **0 - Transbus GTFS** | `transbus_stations/transbus_stops.txt` | City-bus stops with surveyed GPS coordinates; threshold 90 (token sort ratio) |
| **1 - OSM** | `overpass/export.geojson` | Named bus-stop nodes from OpenStreetMap; threshold 72 |
| **2 - Nominatim cache** | `station_finder/nominatim_cache.json` | Pre-computed results - refresh with `nominatim/geocode_nominatim.py` |
| **2b - Manual** | `station_finder/manual_coords.json` | Always applied last (overrides any earlier phase) |
| **3 - OSRM interpolation** | router.project-osrm.org | Places remaining stops along road geometry between anchors (2 passes) |
| **4 - Outlier repair** | Route context | **Interior stops**: detour excess > 15 km relative to in-route neighbors → replaced with OSRM-interpolated position. **Terminal stops**: haversine / timetable-km ratio > 1.5 (physically impossible) → removed; dependent OSRM-interpolated stops cascade-evicted; extra Phase 3 pass re-fills the gap. |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.10+** | Tested on 3.12 |
| **Java 11+** | Required only to run the GTFS validator |
| **pip packages** | See below |

Install Python dependencies inside a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install requests rapidfuzz pypdf rapidocr-onnxruntime numpy Pillow
```

> `rapidocr-onnxruntime`, `numpy`, and `Pillow` are only needed when
> `Program-transport.pdf` contains image-based pages (OCR fallback).  
> If the PDF has selectable text, the OCR packages are never imported.

---

## Running the scripts

### Scenario A - Rebuild after OSM or manual-coords update

Use this when you have updated the OSM export (`overpass/export.geojson`) or changed
`station_finder/manual_coords.json`, but the source PDFs have **not** changed.

The parsed JSON files already exist; only the geocoding and GTFS build need to re-run.

```powershell
# 1. Rebuild the canonical station name index
python station_finder/build_station_set.py

# 2. (Optional) Refresh the Nominatim cache for newly unmatched stops
#    Skip this step if nominatim_cache.json is already up to date.
#    Also regenerate the overpass export (overpass/export.geojson):
python overpass/fetch_overpass.py
python station_finder/nominatim/geocode_nominatim.py

# 3. Re-geocode all stops (uses existing Nominatim cache - no API calls)
python station_finder/geocode_stations.py

# (Optional) Inspect per-route coverage
python build_pipeline/scripts/validate_geocoding.py

# 4. Rebuild the GTFS feed
python build_pipeline/scripts/build_gtfs.py

# (Optional) Generate a GeoJSON preview for geojson.io (stops + route paths)
python build_pipeline/scripts/make_geojson_preview.py
```

> **Nominatim rate limit** - a full cache refresh with `geocode_nominatim.py` and an
> empty cache takes roughly 9 minutes (≤ 1 request/second, per the Nominatim ToS).
> Subsequent runs only query new or forced entries.

To refresh the OSM export, run:

```powershell
python overpass/fetch_overpass.py
```

This POSTs the query in `overpass/request.txt` to the Overpass API and writes
the result to `overpass/export.geojson` in the same GeoJSON format that
Overpass Turbo produces.

---

### Scenario B - Rebuild after new CJ Buzău PDF release

Use this when Consiliul Județean Buzău publishes updated timetable documents.

```powershell
# 0. Replace the source PDFs
#    Copy the new files into raw/:
#      raw/Lista-statiilor.pdf
#      raw/Program-transport.pdf

# 1. Re-parse the station list
python extractors/scripts/schedule_parser.py

# 2. Re-parse the timetables
python extractors/scripts/route_parser.py

# 3. Continue with steps 1-4 from Scenario A
python station_finder/build_station_set.py
python station_finder/nominatim/geocode_nominatim.py   # only needed for new stops
python station_finder/geocode_stations.py
python build_pipeline/scripts/build_gtfs.py
python build_pipeline/scripts/make_geojson_preview.py  # optional - inspect stops + routes
```

> If the new PDFs introduce station names not yet in `station_finder/manual_coords.json`,
> run `python build_pipeline/scripts/validate_geocoding.py` to find routes with low stop
> coverage, then add missing coordinates to `manual_coords.json` and re-run the geocoder.

---

### (Optional) Validate the GTFS feed

Download [gtfs-validator.jar](https://github.com/MobilityData/gtfs-validator/releases)
and place it in the project root, then:

```powershell
java -jar gtfs-validator.jar --input build_pipeline/output/gtfs/ --output_base gtfs_validation/ --country_code RO
```

Results are written to `gtfs_validation/report.html`.

---

## License

MIT License - see [LICENSE](LICENSE).

Source data is based on Consiliul Județean Buzău timetable PDFs.
Project data files are stored and versioned only in this repository, and are used here for public-interest transit tooling.  
OSM data is © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright)).  
Transbus GTFS data sourced from [Mobility Database](https://mobilitydatabase.org/feeds/gtfs/mdb-2106).

## Disclaimer

This project is an independent initiative and is not affiliated with or endorsed by Consiliul Județean Buzău or any other official transit authority.

The generated GTFS feed is provided "as is" for informational and development purposes. While efforts are made to ensure accuracy, there may be errors or omissions in the data. Users should verify critical information with official sources before relying on it for travel planning or operational use.