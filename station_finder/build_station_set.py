"""Extract unique station names from parsed/lista_statiilor.json.

Deduplicates names that differ only in diacritics, capitalisation, or old vs
modern Romanian orthography (î ↔ â) by normalising to a comparison key and
selecting the most-frequently-used form as the canonical name.

Outputs
-------
station_finder/stations.json       - sorted canonical names
station_finder/canonical_map.json  - {every_variant: canonical_name}

Usage
-----
    python station_finder/build_station_set.py
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _normalize(name: str) -> str:
    """Lowercase, strip diacritics, unify old î↔â spelling, collapse whitespace."""
    name = name.strip().rstrip("*").strip()
    # Unify old Romanian î-circumflex with modern â-circumflex (same phoneme).
    name = name.replace("\u00ee", "\u00e2").replace("\u00ce", "\u00c2")
    name = "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", name).lower().strip()


def build_canonical_map(lista_path: Path) -> dict[str, str]:
    """Return {variant → canonical} for every station name in lista_path.

    Groups names by their normalised key.  Within each group the form that
    appears most often across all route station lists is chosen as canonical.
    """
    data = json.loads(lista_path.read_text(encoding="utf-8"))

    counts: Counter[str] = Counter()
    for route in data:
        for station in route.get("stations", []):
            name = station.get("station_name", "").strip()
            if name:
                counts[name] += 1

    groups: dict[str, list[str]] = {}
    for name in counts:
        key = _normalize(name)
        groups.setdefault(key, []).append(name)

    canonical_map: dict[str, str] = {}
    for variants in groups.values():
        canonical = max(variants, key=lambda v: counts[v])
        for v in variants:
            canonical_map[v] = canonical

    return canonical_map


def load_station_names(lista_path: Path) -> set[str]:
    """Return raw (non-deduped) station names - kept for backward compatibility."""
    data = json.loads(lista_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for route in data:
        for station in route.get("stations", []):
            name = station.get("station_name", "").strip()
            if name:
                names.add(name)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build canonical station names from lista_statiilor.json."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / "parsed" / "lista_statiilor.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "stations.json",
    )
    parser.add_argument(
        "--canonical-map",
        dest="canonical_map_path",
        type=Path,
        default=Path(__file__).parent / "canonical_map.json",
    )
    args = parser.parse_args()

    canonical_map = build_canonical_map(args.input)
    canonicals = sorted(set(canonical_map.values()))

    # Report collapsed duplicates
    groups: dict[str, list[str]] = {}
    for variant, canonical in canonical_map.items():
        groups.setdefault(canonical, []).append(variant)
    collapsed = {c: vs for c, vs in groups.items() if len(vs) > 1}
    if collapsed:
        print(f"Collapsed {len(collapsed)} duplicate groups:")
        for canonical, variants in sorted(collapsed.items()):
            others = [v for v in variants if v != canonical]
            print(f"  {canonical!r} \u2190 {others}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(canonicals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    args.canonical_map_path.write_text(
        json.dumps(canonical_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {len(canonicals)} canonical stations \u2192 {args.output}")
    print(f"Wrote canonical_map ({len(canonical_map)} entries) \u2192 {args.canonical_map_path}")


if __name__ == "__main__":
    main()
