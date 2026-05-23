"""Print raw OCR rows for specific routes to diagnose alignment failures."""
import sys, re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from extractors.scripts.route_parser import _iter_rows, _looks_like_route_start, ROUTE_ID_RE

TARGET = {"015", "026", "028", "034", "055"}
INPUT = Path("raw/Program-transport.pdf")

rows = list(_iter_rows(INPUT))

current_route = None
for i, parts in enumerate(rows):
    non_empty = [p for p in parts if p and p.strip()]
    # Detect route start
    if (len(non_empty) >= 3
            and non_empty[0].isdigit()
            and re.fullmatch(r"\d{1,2}", non_empty[1])
            and re.fullmatch(r"\d{3}", non_empty[2])):
        current_route = non_empty[2]

    if current_route in TARGET:
        print("R%s row %d: %s" % (current_route, i, parts))
