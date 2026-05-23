"""Parse Lista-statiilor PDF into route station structures.

Input (default): raw/Lista-statiilor.pdf
Output (default): parsed/lista_statiilor.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
	sys.path.insert(0, str(PROJECT_ROOT))

from extractors.models.raw_route import RawRoute
from extractors.corrections import (
	ROUTE_METADATA_CORRECTIONS,
	ROUTE_STATION_NAME_CORRECTIONS,
	ROUTE_STATION_REMOVALS,
)

HEADER_MARKERS = (
	"nr.",
	"crt",
	"cod",
	"traseu",
	"denumire",
	"autog",
	"plecare",
	"sosire",
	"km",
)


def _normalize_cell(value: str | None) -> str:
	if not value:
		return ""
	return " ".join(value.replace("\n", " ").strip().split())


def _is_route_code(value: str) -> bool:
	return bool(re.fullmatch(r"\d{3}", value.strip()))


def _is_distance(value: str) -> bool:
	normalized = value.strip().replace(",", ".")
	return bool(re.fullmatch(r"\d+(?:\.\d+)?", normalized))


def _normalize_distance(value: str) -> float:
	return float(value.strip().replace(",", "."))


def _sanitize_station_name(name: str) -> str:
	cleaned = " ".join(name.strip().split())
	return re.sub(r"\s+(?=\*+$)", "", cleaned)


def _extract_station_and_distance(parts: list[str]) -> tuple[str, float] | None:
	non_empty = [part for part in parts if part]
	if len(non_empty) < 2:
		return None

	distance_candidate = non_empty[-1]
	if not _is_distance(distance_candidate):
		return None

	station_candidate = _sanitize_station_name(non_empty[-2])
	if not station_candidate:
		return None

	return station_candidate, _normalize_distance(distance_candidate)


def _line_is_header(parts: list[str]) -> bool:
	non_empty = [part for part in parts if part]
	if any(_is_route_code(part) for part in non_empty):
		return False

	joined = " ".join(part.lower() for part in non_empty)
	if not joined:
		return False

	marker_hits = sum(1 for marker in HEADER_MARKERS if marker in joined)
	return marker_hits >= 2


def _try_parse_parent(parts: list[str]) -> tuple[int, str, str, str, str, tuple[str, float] | None] | None:
	non_empty = [part for part in parts if part]
	if len(non_empty) < 5:
		return None

	number_idx = next((i for i, value in enumerate(non_empty) if value.isdigit()), None)
	if number_idx is None or number_idx + 1 >= len(non_empty):
		return None

	number_entry = non_empty[number_idx]
	route_code = non_empty[number_idx + 1]
	if not number_entry.isdigit() or not _is_route_code(route_code):
		return None

	if len(non_empty) <= number_idx + 4:
		return None

	from_location = non_empty[number_idx + 2]
	intermediary_stop = non_empty[number_idx + 3]
	to_location = non_empty[number_idx + 4]

	station_and_distance = None
	if len(parts) >= 7 and parts[5] and parts[6] and _is_distance(parts[6]):
		station_and_distance = (_sanitize_station_name(parts[5]), _normalize_distance(parts[6]))
	elif len(non_empty) >= number_idx + 7 and _is_distance(non_empty[-1]):
		station_and_distance = (_sanitize_station_name(non_empty[-2]), _normalize_distance(non_empty[-1]))

	return (
		int(number_entry),
		route_code,
		from_location,
		intermediary_stop,
		to_location,
		station_and_distance,
	)


def _start_route(parent_data: tuple[int, str, str, str, str, tuple[str, float] | None]) -> RawRoute:
	number_entry, route_code, from_location, intermediary_stop, to_location, first_station = parent_data
	route = RawRoute(
		number_entry=number_entry,
		route_number=route_code,
		from_location=from_location,
		intermediary_stop=intermediary_stop,
		to_location=to_location,
		stations=[],
	)
	if first_station:
		route.stations.append((first_station[0], first_station[1]))
	return route


def _finalize_route(route: RawRoute) -> RawRoute:
	override = ROUTE_METADATA_CORRECTIONS.get(route.route_number)
	if override:
		old_from = route.from_location
		for field, value in override.items():
			setattr(route, field, value)
		new_from = route.from_location
		if route.stations and route.stations[0][0] == old_from and new_from != old_from:
			route.stations[0] = (new_from, route.stations[0][1])

	station_name_overrides = ROUTE_STATION_NAME_CORRECTIONS.get(route.route_number, {})
	if station_name_overrides:
		route.stations = [
			(station_name_overrides.get(station_name, station_name), kilometers)
			for station_name, kilometers in route.stations
		]

	station_removals = ROUTE_STATION_REMOVALS.get(route.route_number)
	if station_removals:
		route.stations = [
			(station_name, kilometers)
			for station_name, kilometers in route.stations
			if (station_name, kilometers) not in station_removals
		]

	return route


def _extract_rows_from_pdf(pdf_path: Path) -> list[list[str]]:
	try:
		from pypdf import PdfReader
	except ModuleNotFoundError as error:
		raise RuntimeError(
			"Missing dependency 'pypdf'. Install it with: pip install pypdf"
		) from error

	parent_pattern = re.compile(r"^(\d+)\s+(\d{3})\s+(.+?)\s+(\d+(?:[.,]\d+)?)$")
	child_pattern = re.compile(r"^(.+?)\s+(\d+(?:[.,]\d+)?)$")

	rows: list[list[str]] = []
	reader = PdfReader(str(pdf_path))
	for page in reader.pages:
		text = page.extract_text() or ""
		for raw_line in text.splitlines():
			line = _normalize_cell(raw_line)
			if not line:
				continue

			parent_match = parent_pattern.match(line)
			if parent_match:
				number_entry, route_code, descriptor, kilometers = parent_match.groups()
				descriptor_tokens = descriptor.split()

				# pypdf emits route descriptor in B-C-A order for this PDF.
				if len(descriptor_tokens) >= 3:
					intermediary_stop = descriptor_tokens[0]
					to_location = descriptor_tokens[1]
					from_location = " ".join(descriptor_tokens[2:])
				elif len(descriptor_tokens) == 2:
					intermediary_stop = ""
					to_location = descriptor_tokens[0]
					from_location = descriptor_tokens[1]
				else:
					intermediary_stop = ""
					to_location = descriptor
					from_location = descriptor

				rows.append(
					[
						number_entry,
						route_code,
						from_location,
						intermediary_stop,
						to_location,
						from_location,
						kilometers,
					]
				)
				continue

			child_match = child_pattern.match(line)
			if child_match:
				station_name, kilometers = child_match.groups()
				rows.append(["", "", "", "", "", station_name, kilometers])

	return rows


def _extract_rows_from_pipe_text(text_path: Path) -> list[list[str]]:
	rows: list[list[str]] = []
	for raw_line in text_path.read_text(encoding="utf-8").splitlines():
		if "|" not in raw_line:
			continue
		parts = [_normalize_cell(part) for part in raw_line.split("|")]
		rows.append(parts)
	return rows


def _iter_rows(input_path: Path) -> Iterable[list[str]]:
	if input_path.suffix.lower() in {".txt", ".csv"}:
		return _extract_rows_from_pipe_text(input_path)
	return _extract_rows_from_pdf(input_path)


def parse_routes(input_path: Path) -> list[RawRoute]:
	rows = list(_iter_rows(input_path))

	routes: list[RawRoute] = []
	current_route: RawRoute | None = None
	started = False

	for parts in rows:
		parts = [_normalize_cell(part) for part in parts]
		non_empty = [part for part in parts if part]
		if not non_empty:
			continue

		if _line_is_header(parts):
			continue

		parent_data = _try_parse_parent(parts)

		if parent_data:
			started = True
			if current_route is not None:
				routes.append(_finalize_route(current_route))

			current_route = _start_route(parent_data)

			continue

		route_only_candidate = next((part for part in non_empty if _is_route_code(part)), None)
		if not started and route_only_candidate:
			started = True

		if route_only_candidate and len(non_empty) == 1:
			continue

		if not started:
			continue

		if current_route is None:
			# Glued route-header artifacts with no usable parent metadata: keep waiting.
			continue

		station_and_distance = _extract_station_and_distance(parts)
		if station_and_distance is None:
			continue

		current_route.stations.append((station_and_distance[0], station_and_distance[1]))

	if current_route is not None:
		routes.append(_finalize_route(current_route))

	return routes


def _route_to_dict(route: RawRoute) -> dict:
	return {
		"number_entry": route.number_entry,
		"route_number": route.route_number,
		"from_location": route.from_location,
		"intermediary_stop": route.intermediary_stop,
		"to_location": route.to_location,
		"stations": [
			{
				"station_name": station_name,
				"kilometers_from_previous_station": kilometers,
			}
			for station_name, kilometers in route.stations
		],
	}


def main() -> None:
	parser = argparse.ArgumentParser(description="Parse Lista-statiilor into normalized JSON.")
	parser.add_argument(
		"--input",
		type=Path,
		default=PROJECT_ROOT / "raw" / "Lista-statiilor.pdf",
		help="Path to source file (.pdf or .txt with '|' delimiter)",
	)
	parser.add_argument(
		"--output",
		type=Path,
		default=PROJECT_ROOT / "parsed" / "lista_statiilor.json",
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
