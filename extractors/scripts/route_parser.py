"""Parse Program-transport PDF into route schedule structures.

Input (default): raw/Program-transport.pdf
Output (default): parsed/program_transport.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Iterable

# Bump this constant whenever _normalize_cell changes so that stale OCR cache
# files are automatically bypassed and a fresh OCR pass is triggered.
_OCR_CACHE_VERSION = 1

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from extractors.corrections import (
    ROUTE_METADATA_CORRECTIONS,
    NOTE_TEXT_CORRECTIONS,
    ROUTE_SCHEDULE_CORRECTIONS,
)
from extractors.models.raw_schedule import RawSchedule, RawScheduleRow

TIME_RE = re.compile(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b")
ROUTE_ID_RE = re.compile(r"^\d{3}$")
FUSED_BACK_RE = re.compile(r"(\d{2}:\d{2})(?:\s+([\d,]+))?(?:\s+(.*?))?(?=\s+\d{2}:\d{2}|$)")
SCHOOL_RE = re.compile(r"școlar|scolar|şcolar|cursuri", re.IGNORECASE)
NOTE_KEYWORD_RE = re.compile(r"circu(?:l[aă]|lă)|sensul", re.IGNORECASE)
NOISE_LINE_RE = re.compile(r"\b(hcj|grupa|program\s+de\s+transport|consiliul\s+judetean)\b", re.IGNORECASE)


@dataclass
class BackToken:
    time: str
    days_raw: str | None
    notes: str | None


def _normalize_cell(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\n", " ").strip()
    # OCR drops leading zeros from single-digit hours: '7:30' → '07:30'.
    text = re.sub(r"\b([1-9]):([0-5]\d)\b", r"0\1:\2", text)
    # OCR sometimes renders the colon as a dot in times: '07.30' → '07:30'.
    text = re.sub(r"\b(\d{2})\.([0-5]\d)\b", r"\1:\2", text)
    return " ".join(text.split())


def _normalize_line(value: str) -> str:
    return " ".join(value.replace("\t", " ").strip().split())


def _to_int(value: str, default: int = 0) -> int:
    match = re.search(r"\d+", value)
    if not match:
        return default
    return int(match.group(0))


def _to_minutes(value: str) -> int | None:
    if not TIME_RE.fullmatch(value):
        return None
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _ocr_extract_rows(reader: object) -> list[list[str]]:
    """Fall-back: OCR every embedded page image and reconstruct rows spatially.

    The schedule table in Program-transport.pdf is stored rotated 90° CCW.
    A 90° CW rotation restores the natural reading order so that each route
    occupies a horizontal row with metadata on the left and times on the right.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image
        import numpy as np
        from io import BytesIO
    except ImportError as exc:
        raise RuntimeError(
            "OCR fallback requires rapidocr-onnxruntime, numpy, and Pillow. "
            "Install them with: pip install rapidocr-onnxruntime numpy Pillow"
        ) from exc

    ocr = RapidOCR()
    rows: list[list[str]] = []

    for page in reader.pages:  # type: ignore[attr-defined]
        for img_obj in list(page.images):
            from io import BytesIO as _BytesIO
            img = Image.open(_BytesIO(img_obj.data)).convert("RGB")
            # Rotate 90° CW (negative angle in PIL's CCW convention) so the
            # table's column-header column appears on the left of each row.
            img = img.rotate(-90, expand=True)
            result, _ = ocr(np.array(img))
            if not result:
                continue

            tokens: list[tuple[float, float, str]] = []
            for box, text, conf in result:
                y = min(pt[1] for pt in box)
                x = min(pt[0] for pt in box)
                clean = _normalize_cell(text)
                if clean and not NOISE_LINE_RE.search(clean):
                    tokens.append((y, x, clean))

            tokens.sort()

            # Group tokens whose top-y is within 20 px of the running group
            grouped: list[list] = []
            for y, x, text in tokens:
                if not grouped or abs(y - grouped[-1][0]) > 20:
                    grouped.append([y, [(x, text)]])
                else:
                    grouped[-1][1].append((x, text))

            for entry in grouped:
                toks_sorted = sorted(entry[1], key=lambda t: t[0])
                parts = [t for _, t in toks_sorted]
                rows.append(parts)

    return rows


def _extract_pipe_rows_from_pdf(pdf_path: Path) -> list[list[str]]:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as error:
        raise RuntimeError("Missing dependency 'pypdf'. Install it with: pip install pypdf") from error

    rows: list[list[str]] = []
    reader = PdfReader(str(pdf_path))
    for page in reader.pages:
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = _normalize_line(raw_line)
            if not line or NOISE_LINE_RE.search(line):
                continue
            if "|" not in line:
                continue
            parts = [_normalize_cell(part) for part in line.split("|")]
            rows.append(parts)

    if not rows:
        rows = _ocr_extract_rows_cached(pdf_path, reader)

    return rows


def _ocr_extract_rows_cached(pdf_path: Path, reader: object) -> list[list[str]]:
    """Thin cache layer around _ocr_extract_rows.

    Results are stored as JSON in ``<pdf_dir>/.ocr_cache/``, keyed by the
    SHA-256 of the PDF content and *_OCR_CACHE_VERSION*.  Bump the version
    constant whenever _normalize_cell changes to invalidate stale caches.
    """
    content_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
    cache_dir = pdf_path.parent / ".ocr_cache"
    cache_file = cache_dir / f"{pdf_path.stem}_{content_hash}_v{_OCR_CACHE_VERSION}.json"

    if cache_file.exists():
        print(f"Loading OCR rows from cache ({cache_file.name})…", file=sys.stderr)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    print("OCR pass starting (this may take 2–3 minutes)…", file=sys.stderr)
    rows = _ocr_extract_rows(reader)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"OCR rows cached → {cache_file}", file=sys.stderr)
    return rows


def _extract_rows_from_pipe_text(text_path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_line in text_path.read_text(encoding="utf-8").splitlines():
        line = _normalize_line(raw_line)
        if not line or NOISE_LINE_RE.search(line):
            continue
        if "|" not in line:
            continue
        parts = [_normalize_cell(part) for part in line.split("|")]
        rows.append(parts)
    return rows


def _iter_rows(input_path: Path) -> Iterable[list[str]]:
    if input_path.suffix.lower() in {".txt", ".csv"}:
        return _extract_rows_from_pipe_text(input_path)
    return _extract_pipe_rows_from_pdf(input_path)


def _looks_like_route_start(parts: list[str]) -> bool:
    non_empty = [part for part in parts if part]
    if len(non_empty) < 3:
        return False

    return (
        non_empty[0].isdigit()
        and bool(re.fullmatch(r"\d{1,2}", non_empty[1]))
        and bool(ROUTE_ID_RE.fullmatch(non_empty[2]))
    )


def _looks_like_overflow(parts: list[str]) -> bool:
    prefix = parts[:6]
    if any(part for part in prefix):
        return False
    tail = " ".join(part for part in parts[6:] if part)
    return bool(TIME_RE.search(tail))


def _parse_days(days_raw: str | None) -> list[int]:
    if not days_raw:
        return [1, 2, 3, 4, 5, 6, 7]
    # Normalize: treat dots as comma separators (common OCR artifact).
    normalized = re.sub(r"\.", ",", days_raw)
    values: list[int] = []
    for token in normalized.split(","):
        token = token.strip()
        if not token.isdigit():
            continue
        value = int(token)
        if 1 <= value <= 7:
            values.append(value)
    return values or [1, 2, 3, 4, 5, 6, 7]


def _split_days_note(text: str) -> tuple[str, str]:
    """Split a token that may combine a days-of-operation prefix with a note.

    The PDF uses asterisks as footnote markers so OCR may produce tokens like
    ``'1,2,3,4,5*circulainperioadacursurilorscolare'``.
    Dots are substituted for commas in the days part (OCR artifact).

    Returns:
        (days_raw, note_text) — either may be empty string.
    """
    m = re.match(r"^([\d][,.\d]*)(.*)$", text)
    if not m:
        return "", text.lstrip("* ").strip()
    days_raw = re.sub(r"\.", ",", m.group(1).rstrip(","))
    note_text = m.group(2).lstrip("* ").strip()
    return days_raw, note_text


def _extract_back_tokens(text: str) -> list[BackToken]:
    clean = _normalize_cell(text)
    if not clean:
        return []

    matches = list(FUSED_BACK_RE.finditer(clean))
    if matches:
        tokens: list[BackToken] = []
        for match in matches:
            time = match.group(1)
            days_raw = match.group(2)
            notes = _normalize_cell(match.group(3) or "")
            tokens.append(BackToken(time=time, days_raw=days_raw, notes=notes or None))
        return tokens

    return [BackToken(time=t, days_raw=None, notes=None) for t in TIME_RE.findall(clean)]


def _duration_minutes(departure: str | None, arrival: str | None) -> int | None:
    if not departure or not arrival:
        return None
    dep = _to_minutes(departure)
    arr = _to_minutes(arrival)
    if dep is None or arr is None:
        return None
    return (arr - dep) % 1440


def _resolve_back_pair(t1: str | None, t2: str | None, expected_duration: int | None) -> tuple[str | None, str | None]:
    if not t1 and not t2:
        return None, None
    if not t1:
        return t2, None
    if not t2:
        return t1, None

    m1 = _to_minutes(t1)
    m2 = _to_minutes(t2)
    if m1 is None or m2 is None:
        return t1, t2

    d12 = (m2 - m1) % 1440
    d21 = (m1 - m2) % 1440

    if expected_duration is not None:
        if abs(d12 - expected_duration) <= abs(d21 - expected_duration):
            return t1, t2
        return t2, t1

    # Without a known duration, treat the earlier clock time as departure,
    # except for obvious overnight wraps where late-evening to early-morning
    # should remain in original order.
    if m1 <= m2:
        return t1, t2
    if (m1 - m2) > 720:
        return t1, t2
    return t2, t1


def _metadata_from_parts(parts: list[str]) -> tuple[int, int, str, str, str, str, int, int] | None:
    non_empty = [part for part in parts if part]
    if len(non_empty) < 8:
        return None

    network = non_empty[0]
    group = non_empty[1]
    route = non_empty[2]

    if not network.isdigit() or not re.fullmatch(r"\d{1,2}", group) or not ROUTE_ID_RE.fullmatch(route):
        return None

    from_location = non_empty[3]
    # Detect whether an intermediary stop is present.
    # If non_empty[5] is a pure integer it is the km value → no intermediary.
    # This works for both pipe-delimited rows (where empty cells are excluded from
    # non_empty) and OCR-reconstructed rows (where empty cells never existed).
    has_intermediary = len(non_empty) > 5 and not re.fullmatch(r"\d+", non_empty[5])

    if has_intermediary:
        intermediary = non_empty[4]
        to_location = non_empty[5] if len(non_empty) > 5 else ""
        km_value = non_empty[6] if len(non_empty) > 6 else "0"
        trips_value = non_empty[7] if len(non_empty) > 7 else "0"
    else:
        intermediary = ""
        to_location = non_empty[4]
        km_value = non_empty[5] if len(non_empty) > 5 else "0"
        trips_value = non_empty[6] if len(non_empty) > 6 else "0"

    return (
        _to_int(network),
        _to_int(group),
        route,
        _normalize_cell(from_location),
        _normalize_cell(intermediary),
        _normalize_cell(to_location),
        _to_int(km_value),
        _to_int(trips_value),
    )


def _extract_schedule_columns(parts: list[str]) -> tuple[list[str], list[str], list[BackToken]]:
    # Locate the first time token after the 8 metadata columns.
    # For pipe-delimited rows this is typically at index 11;
    # for OCR rows without an intermediary stop it may be at index 10.
    sched_start = next((i for i in range(8, len(parts)) if TIME_RE.search(parts[i])), len(parts))

    if sched_start < len(parts):
        # Long row (route start or pipe-delimited): schedule begins at sched_start.
        way_dep_text = parts[sched_start]
        way_arr_text = parts[sched_start + 1] if sched_start + 1 < len(parts) else ""
        back_segments: list[str] = list(parts[sched_start + 2:])
    else:
        # Short OCR overflow row: all tokens are schedule data.
        # Scan left-to-right: 1st time = way_dep, 2nd = way_arr, rest = back data.
        way_dep_text = ""
        way_arr_text = ""
        back_segments = []
        time_count = 0
        for p in parts:
            if not p:
                continue
            if TIME_RE.search(p):
                time_count += 1
                if time_count == 1:
                    way_dep_text = p
                elif time_count == 2:
                    way_arr_text = p
                else:
                    back_segments.append(p)
            elif time_count >= 2:
                # Non-time token after the 2nd way time: may be days/notes.
                back_segments.append(p)

    way_departures = TIME_RE.findall(way_dep_text)
    way_arrivals = TIME_RE.findall(way_arr_text)

    back_tokens: list[BackToken] = []
    for segment in back_segments:
        if TIME_RE.search(segment):
            # Time-containing segment: use FUSED_BACK_RE for pipe-delimited fused values.
            back_tokens.extend(_extract_back_tokens(segment))
            continue

        # May be days-only, mixed days+note (e.g. "1,2,3,4,5*circulapana..."), or pure note.
        days_part, note_text = _split_days_note(segment)

        if days_part and back_tokens:
            # Attach days to the last back token that has no days yet.
            for i in range(len(back_tokens) - 1, -1, -1):
                if back_tokens[i].days_raw is None:
                    back_tokens[i] = BackToken(
                        back_tokens[i].time,
                        days_raw=days_part,
                        notes=back_tokens[i].notes,
                    )
                    break

        if note_text and back_tokens:
            # Attach note to the last back token.
            last = back_tokens[-1]
            combined = (last.notes + " " + note_text).strip() if last.notes else note_text
            back_tokens[-1] = BackToken(last.time, days_raw=last.days_raw, notes=combined)

    return way_departures, way_arrivals, back_tokens


def _build_schedule_entries(
    way_departures: list[str],
    way_arrivals: list[str],
    back_tokens: list[BackToken],
) -> list[RawScheduleRow]:
    expected_duration: int | None = None
    for dep, arr in zip(way_departures, way_arrivals):
        duration = _duration_minutes(dep, arr)
        if duration is not None:
            expected_duration = duration
            break

    back_pairs: list[tuple[str | None, str | None, str | None, str | None]] = []
    i = 0
    while i < len(back_tokens):
        first = back_tokens[i]
        second = back_tokens[i + 1] if i + 1 < len(back_tokens) else None

        dep, arr = _resolve_back_pair(first.time, second.time if second else None, expected_duration)
        days_raw = first.days_raw or (second.days_raw if second else None)
        notes = first.notes or (second.notes if second else None)

        back_pairs.append((dep, arr, days_raw, notes))
        i += 2

    entries: list[RawScheduleRow] = []
    for way_dep, way_arr, back_info in zip_longest(
        way_departures,
        way_arrivals,
        back_pairs,
        fillvalue=None,
    ):
        back_dep: str | None = None
        back_arr: str | None = None
        days_raw: str | None = None
        notes: str | None = None
        if back_info is not None:
            back_dep, back_arr, days_raw, notes = back_info

        entries.append(
            RawScheduleRow(
                way_departure=way_dep,
                way_arrival=way_arr,
                back_departure=back_dep,
                back_arrival=back_arr,
                days_of_operation=_parse_days(days_raw),
                only_for_school_days=bool(SCHOOL_RE.search(notes or "")),
                notes=notes,
            )
        )

    return entries


def parse_routes(input_path: Path) -> list[RawSchedule]:
    rows = list(_iter_rows(input_path))

    routes: list[RawSchedule] = []
    current_meta: tuple[int, int, str, str, str, str, int, int] | None = None
    current_way_departures: list[str] = []
    current_way_arrivals: list[str] = []
    current_back_tokens: list[BackToken] = []

    def flush_current() -> None:
        nonlocal current_meta, current_way_departures, current_way_arrivals, current_back_tokens
        if current_meta is None:
            return

        entries = _build_schedule_entries(
            way_departures=current_way_departures,
            way_arrivals=current_way_arrivals,
            back_tokens=current_back_tokens,
        )

        routes.append(
            RawSchedule(
                network_id=current_meta[0],
                group_id=current_meta[1],
                route_number=current_meta[2],
                from_location=current_meta[3],
                intermediary_stop=current_meta[4],
                to_location=current_meta[5],
                kilometers=current_meta[6],
                planned_trips=current_meta[7],
                schedule_entries=entries,
            )
        )

        current_meta = None
        current_way_departures = []
        current_way_arrivals = []
        current_back_tokens = []

    for parts in rows:
        parts = [_normalize_cell(part) for part in parts]
        if not any(parts):
            continue

        if _looks_like_route_start(parts):
            flush_current()
            metadata = _metadata_from_parts(parts)
            if metadata is None:
                continue

            current_meta = metadata
            way_dep, way_arr, back = _extract_schedule_columns(parts)
            current_way_departures.extend(way_dep)
            current_way_arrivals.extend(way_arr)
            current_back_tokens.extend(back)
            continue

        if current_meta is None:
            continue

        row_has_time = any(TIME_RE.search(part) for part in parts)

        if not row_has_time:
            # Annotation-only row (no times): may carry days or note text belonging
            # to the most-recently accumulated back token of the current route.
            row_text = " ".join(p for p in parts if p).strip()
            if row_text and current_back_tokens:
                days_part, note_text = _split_days_note(row_text)

                if days_part:
                    for i in range(len(current_back_tokens) - 1, -1, -1):
                        if current_back_tokens[i].days_raw is None:
                            current_back_tokens[i] = BackToken(
                                current_back_tokens[i].time,
                                days_raw=days_part,
                                notes=current_back_tokens[i].notes,
                            )
                            break

                if note_text and (
                    SCHOOL_RE.search(note_text) or NOTE_KEYWORD_RE.search(note_text)
                ):
                    last = current_back_tokens[-1]
                    combined = (last.notes + " " + note_text).strip() if last.notes else note_text
                    current_back_tokens[-1] = BackToken(last.time, days_raw=last.days_raw, notes=combined)
            continue

        # For OCR rows there is no empty-prefix sentinel; accept any row that
        # contains a time token and was not already handled as a route start.
        if _looks_like_overflow(parts) or row_has_time:
            way_dep, way_arr, back = _extract_schedule_columns(parts)
            current_way_departures.extend(way_dep)
            current_way_arrivals.extend(way_arr)
            current_back_tokens.extend(back)

    flush_current()

    # Apply known metadata corrections (location names, etc.).
    for route in routes:
        overrides = ROUTE_METADATA_CORRECTIONS.get(route.route_number, {})
        for field, value in overrides.items():
            setattr(route, field, value)

    # Clean up note text: filter OCR time-fragment artifacts and normalise to
    # proper Romanian.  Re-evaluate only_for_school_days after text changes.
    for route in routes:
        for entry in route.schedule_entries:
            note = entry.notes
            if not note:
                continue
            # Drop truncated-time artifacts (e.g. ':00', ':60') that contain
            # no alphabetic characters.
            if not any(c.isalpha() for c in note):
                entry.notes = None
                continue
            cleaned = NOTE_TEXT_CORRECTIONS.get(note, note)
            if cleaned != note:
                entry.notes = cleaned
                entry.only_for_school_days = bool(SCHOOL_RE.search(cleaned))

    # Apply explicit trip-level overrides and OCR-missed trip insertions.
    for route in routes:
        pending_inserts: list[tuple[int | None, RawScheduleRow]] = []

        for correction in ROUTE_SCHEDULE_CORRECTIONS.get(route.route_number, []):
            action = correction.get("action", "update")

            if action == "insert":
                new_entry = RawScheduleRow(
                    way_departure=correction.get("way_departure"),
                    way_arrival=correction.get("way_arrival"),
                    back_departure=correction.get("back_departure"),
                    back_arrival=correction.get("back_arrival"),
                    days_of_operation=correction.get("days_of_operation", [1, 2, 3, 4, 5, 6, 7]),
                    only_for_school_days=correction.get("only_for_school_days", False),
                    notes=correction.get("notes"),
                )
                insert_before = correction.get("insert_before")
                idx: int | None = None
                if insert_before is not None:
                    idx = next(
                        (i for i, e in enumerate(route.schedule_entries)
                         if e.way_departure == insert_before),
                        None,
                    )
                pending_inserts.append((idx, new_entry))
            else:
                target_dep = correction.get("way_departure")
                target_arr = correction.get("way_arrival")
                for entry in route.schedule_entries:
                    if entry.way_departure == target_dep:
                        if target_arr is not None and entry.way_arrival != target_arr:
                            continue
                        for field, value in correction.items():
                            if field not in ("way_departure", "way_arrival", "action"):
                                setattr(entry, field, value)

        # Insert from highest index to lowest so earlier inserts don't shift
        # the positions of later ones.  Append-only entries come last.
        indexed = sorted(
            [(idx, e) for idx, e in pending_inserts if idx is not None],
            key=lambda x: -x[0],
        )
        appended = [e for idx, e in pending_inserts if idx is None]
        for idx, entry in indexed:
            route.schedule_entries.insert(idx, entry)
        for entry in appended:
            route.schedule_entries.append(entry)

    return routes


def _row_to_dict(row: RawScheduleRow) -> dict:
    return {
        "way_departure": row.way_departure,
        "way_arrival": row.way_arrival,
        "back_departure": row.back_departure,
        "back_arrival": row.back_arrival,
        "days_of_operation": row.days_of_operation,
        "only_for_school_days": row.only_for_school_days,
        "notes": row.notes,
    }


def _route_to_dict(route: RawSchedule) -> dict:
    return {
        "network_id": route.network_id,
        "group_id": route.group_id,
        "route_number": route.route_number,
        "from_location": route.from_location,
        "intermediary_stop": route.intermediary_stop,
        "to_location": route.to_location,
        "kilometers": route.kilometers,
        "planned_trips": route.planned_trips,
        "schedule_entries": [_row_to_dict(row) for row in route.schedule_entries],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Program-transport into normalized JSON.")
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / "raw" / "Program-transport.pdf",
        help="Path to source file (.pdf or .txt with '|' delimiter)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "parsed" / "program_transport.json",
        help="Path to output JSON file",
    )
    args = parser.parse_args()

    routes = parse_routes(args.input)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([_route_to_dict(route) for route in routes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Parsed {len(routes)} routes -> {args.output}")


if __name__ == "__main__":
    main()
