# Parsing Specification: Lista-statiilor_2.pdf

## 1. Document Structure & State Management
* Delimiter: The tabular data is strictly separated by the pipe character `|`.
* Stateful Inheritance: The document relies on a parent-child row relationship. 
* Parent Rows: Define a new route and populate the first 5 columns (including the `Cod traseu` in column index 1).
* Child Rows: Belong to the last declared route. They typically leave the first 5 columns blank (represented by empty strings when split) and only contain data for the station name and distance.

## 2. Standard Column Mapping
When a cleanly formatted row is split by `|`, the data maps as follows:
* Index 0: Row Number (`Nr. crt.`).
* Index 1: Route Code (`Cod traseu` - e.g., "001").
* Index 2: Departure Location (`Autogloc/. plecare`).
* Index 3: Intermediate Location (`Loc. intermed.`).
* Index 4: Arrival Location (`Autog./loc. sosire`).
* Index 5: Station Name (`Denumire staţii`).
* Index 6: Distance in Km (`Km`).

## 3. Critical PDF OCR Anomalies (Edge Cases)
The agent must build logic to handle these specific extraction errors:

* Floating Text Artifacts / PDF Page-Header Continuation Lines: The PDF embeds running-header lines at page breaks that re-state the last visible stop and a cumulative kilometre value (e.g., `Popas Merei | 10`). These look identical to real child rows and are emitted by pypdf in the middle of a route's station block, not just before the table starts. Known artifacts that must be unconditionally filtered from every route's station list:
* Floating Text Artifacts / PDF Page-Header Continuation Lines: The PDF embeds running-header lines at page breaks that re-state the last visible stop and a cumulative kilometre value. These look identical to real child rows and are emitted by pypdf in the middle of a route's station block. Known artifacts are listed in `extractors/corrections.py` (`PDF_PAGE_ARTIFACTS`) and must be unconditionally filtered from every route's station list.
* Glued/Merged Route Headers: Due to PDF line-break issues, multiple parent route identifiers are sometimes glued together on consecutive lines before their respective station data appears (e.g., `012` followed immediately by `013`, or `015` and `016`, `042` and `043`, `044` and `045`, `054` and `055`, `060` and `062`). The parser needs to handle these stacked headers correctly.
* Glued/Merged Route Headers: Due to PDF line-break issues, multiple parent route identifiers are sometimes glued together on consecutive lines before their respective station data appears (e.g., `012`/`013`, `015`/`016`, `042`/`043`, `044`/`045`, `054`/`055`, `060`/`062`). This, combined with multi-word location names being split across the from/intermediary/to columns, mangles the `from_location`, `intermediary_stop`, and `to_location` fields. All affected routes and their correct values are maintained in `extractors/corrections.py` (`ROUTE_METADATA_CORRECTIONS`). When a route's `from_location` is overridden, the first entry in its `stations` list must also be updated to the corrected name.
* Shifted Column Indices: From Route 53 onward, the OCR introduces extraneous pipe characters, pushing the station names and distances to the right (e.g., ` |  |  |  |  |  | Gura Bâscei | 1.2`). The agent MUST NOT rely on hardcoded indices 5 and 6 for child rows. It must filter out empty columns and grab the last two valid string elements.
* Inconsistent Decimal Separators: The `Km` column usually uses a period (`1.4`), but occasionally uses a comma (`Sudiţi | 1,0`). These must be normalized to periods.
* Missing Decimals: Some distances are represented as whole numbers without decimals (e.g., `0`, `4`).
* Trailing Asterisks: Some station names include trailing asterisks (e.g., `Vizireni*`, `Ruseţu 3*`) which need to be sanitized.