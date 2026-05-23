# Agent Specification: PDF Transport Schedule Parser

## 1. Objective
Implement a robust state-machine parser using `PdfReader` to extract transport route metadata and schedule entries from **Program-transport.pdf** into the `RawSchedule` and `RawScheduleRow` classes[cite: 5]. The parser must handle PDF table extraction quirks, including text wrapping, column interleaving, vertical inversion, and fused cell strings[cite: 5].

---

## 2. Parsing Strategy: Block-Based State Machine
The parser must not rely on row-by-row CSV logic, as PDF extraction creates fragmented text.

*   **Trigger Condition**: A new `RawSchedule` block is initialized when a line starts with a valid Network ID pattern (e.g., `16 | 01 | 001`)[cite: 5].
*   **State Accumulation**: Maintain an active route object until a new Network ID is encountered.
*   **Overflow Handling**: Detect lines starting with a sequence of empty pipe operators (e.g., ` |  |  |  |  | `). Append time strings from these lines to the active route's schedule arrays[cite: 5].

---

## 3. Metadata Extraction (Columns 1-8)
*   **Cleaning Multiline Cells**: Locations in columns 4, 5, and 6 often contain `\n` characters from wrapped text. Apply `.replace('\n', ' ').strip()` to all location fields[cite: 5].
*   **Intermediary Stops**: If Column 5 is empty, represent as `""` or `None`.
*   **Casting**: Cast Column 7 (`kilometers`) and Column 8 (`planned_trips`) to integers.

---

## 4. Schedule Extraction (Columns 12+)
Schedule data is split into "Way" (Dus) and "Back" (Întors) trips.

### A. "Way" Trips (Dus) - Columns 12 & 13
*   Extract all `HH:MM` patterns from Column 12 as `way_departure` and Column 13 as `way_arrival`.
*   Calculate the trip duration (`way_arrival - way_departure`) in minutes for the first valid trip. This duration is required to disambiguate the "Back" trip order[cite: 5].

### B. "Back" Trips (Întors) - The Inversion & Fusion Logic
*   **Fused Strings**: Data is frequently merged, such as `"08:10 1,2,3,4,5,6 circulă până la Smeeni"`. 
    *   *Parser Rule*: Apply the regex `r"(\d{2}:\d{2})(?:\s+([\d,]+))?(?:\s+(.*))?"` to extract `time`, `days_of_operation`, and `notes`[cite: 5].
*   **Vertical Inversion**: The PDF may place `back_arrival` above `back_departure`.
    *   *Correction Logic*: Compare the time pair ($T_1, T_2$) against the pre-calculated trip duration. Assign the earlier time as `back_departure` unless the duration shift wraps past midnight[cite: 5].
*   **Asymmetrical Trips**: Use `itertools.zip_longest(fillvalue=None)` to combine "Way" and "Back" arrays. A route may have more back trips than way trips (e.g., Route 018)[cite: 5].

---

## 5. Boolean & Data Transformation
*   **Days of Operation**: Split the string capture (e.g., `"1,2,3,4,5"`) by commas into a `List[int]`. If the capture is null, default to `[1, 2, 3, 4, 5, 6, 7]`.
*   **School Days Flag**: Parse the `notes` field for Romanian keywords indicating school-specific operation.
    *   *Logic*: `only_for_school_days = bool(re.search(r"școlar|scolar|şcolar", notes, re.IGNORECASE))`[cite: 5].

---

## 6. Known PDF Quirks to Handle
*   **Varying Column Widths**: Some rows consolidate columns (e.g., Route 005 and 006 have empty intermediary stops, but the pipe count remains constant)[cite: 5].
*   **Typographical Variants**: Account for diacritic variations in the source text (e.g., "Sähäteni" vs "Săhăteni")[cite: 5].
*   **Footer Interruption**: Note that the table is periodically interrupted by page-level meta-comments (e.g., "(grupa 03), HCJ nr. 159/2024"). The parser must discard these lines to prevent them from being injected into schedule arrays[cite: 5].