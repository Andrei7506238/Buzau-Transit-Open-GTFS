"""Known OCR/gluing corrections for Lista-statiilor.pdf.

These tables are the single source of truth for all hard-coded data fixes.
Add new entries here when additional corruptions are discovered; no parser
logic needs to change.
"""

from __future__ import annotations

# Station rows preserved from the PDF, including asterisks and page-break
# continuations, are parsed directly from the table layout.
PDF_PAGE_ARTIFACTS: frozenset[tuple[str, float]] = frozenset()

# Route-specific station spellings that should stay faithful to the PDF source,
# even when the PDF text extraction normalizes them to a more common Romanian
# spelling.
ROUTE_STATION_NAME_CORRECTIONS: dict[str, dict[str, str]] = {
	"004": {"Proșca": "Prosca"},
	"005": {"Vîlcele": "Vilcele"},
	"006": {"Găgeni": "Gägeni"},
}

# Route-specific station rows that the source PDF omits from a given route.
ROUTE_STATION_REMOVALS: dict[str, set[tuple[str, float]]] = {
	"001": {("Popas Merei", 10.0)},
}

# Routes whose parent-row metadata was mangled by PDF OCR gluing consecutive route
# headers together, or by multi-word location names being split across the
# from/intermediary/to columns.  Only the fields that differ from the parsed value
# are listed; unmentioned fields are left as-is.
ROUTE_METADATA_CORRECTIONS: dict[str, dict[str, str]] = {
	"012": {"intermediary_stop": "Căldărușanca", "to_location": "Ileana"},
	"013": {"intermediary_stop": "", "to_location": "Satu Nou"},
	"005": {"from_location": "Buzău Autogara Sud", "intermediary_stop": "", "to_location": "Clondiru"},
	"006": {"from_location": "Buzău Autogara Sud", "intermediary_stop": "", "to_location": "Vintileanca"},
	"016": {"from_location": "Buzău Autogara Sud", "intermediary_stop": "", "to_location": "Padina"},
	"022": {"from_location": "Buzău Autogara XXL", "intermediary_stop": "Poşta Câlnău", "to_location": "Rm. Sărat Autogara Valman Tur"},
	"027": {"from_location": "Buzău Autogara XXL", "intermediary_stop": "", "to_location": "Podu Muncii"},
	"038": {"from_location": "Buzău Autogara XXL", "to_location": "Glodu Petcari"},
	"040": {"from_location": "Buzău Autogara XXL", "to_location": "Golul Grabicina"},
	"043": {"from_location": "Buzău Autogara XXL", "to_location": "Piatra Albă"},
	"045": {"from_location": "Buzău Autogara XXL", "intermediary_stop": "", "to_location": "Nehoiu"},
	"046": {"from_location": "Buzău Autogara XXL", "intermediary_stop": "", "to_location": "Varlaam"},
	"047": {"from_location": "Buzău Autogara XXL", "intermediary_stop": "", "to_location": "Bâsca Chiojdului"},
	"051": {"from_location": "Pătârlagele Autogara", "to_location": "Lunca Jarștei"},
	"056": {"from_location": "Rm. Sărat Autogara TUC SA", "intermediary_stop": "", "to_location": "I. H. Rădulescu"},
	"058": {"from_location": "Rm. Sărat Autogara TUC SA", "intermediary_stop": "", "to_location": "Bălăceanu"},
	"059": {"from_location": "Rm. Sărat Autogara TUC SA", "intermediary_stop": "", "to_location": "Ghergheasa"},
	"053": {"from_location": "Nehoiu", "intermediary_stop": "", "to_location": "Bâsca Chiojdului"},
	"054": {"from_location": "Nehoiu", "intermediary_stop": "", "to_location": "Bâscenii de Sus"},
	"055": {"from_location": "Rm. Sărat Autogara TUC SA", "to_location": "Vâlcelele"},
	"062": {"from_location": "Rm. Sărat Autogara TUC SA", "to_location": "Mucești-Dănulești"},
	"063": {"intermediary_stop": "", "to_location": "Valea Salciei"},
}

# Mapping of raw OCR note strings → clean Romanian text.  All OCR variants of the
# same annotation (case, spacing, missing diacritics, merged words) are listed
# separately so that no regex magic is needed in the parser.
NOTE_TEXT_CORRECTIONS: dict[str, str] = {
	# School-days annotation - many OCR variants of the same phrase.
	"Circulainperioadacursurilorscolare":                  "circulă în perioada cursurilor școlare",
	"circula inperioadacursurilor scolare":                "circulă în perioada cursurilor școlare",
	"circulainperioada cursurilorscolare":                 "circulă în perioada cursurilor școlare",
	"circulainperioadacursurilorcolare":                   "circulă în perioada cursurilor școlare",
	"circulainperioadacursurilorscolar":                   "circulă în perioada cursurilor școlare",
	"circulainperioadacursurilorscolare":                  "circulă în perioada cursurilor școlare",
	# Compound: routing note + school-days (preserve both, separated by semicolon).
	"circulapanalaVaduSoresti circula inperioadacursurilorscolare":
		"circulă până la Vadul Sorești; circulă în perioada cursurilor școlare",
	# Routing notes.
	"ciculaprinVizireni":                "circulă prin Vizireni",
	"circulaprinVizireni":               "circulă prin Vizireni",
	"circula delaCampulungeanca":        "circulă de la Câmpulungeanca",
	"circulapanalaBudaCraciunesti":      "circulă până la Buda-Crăciunești",
	"circulapanalaCampulungeanca":       "circulă până la Câmpulungeanca",
	"circulapanalaRacoviteni":           "circulă până la Racovițeni",
	"circulapanalaSmeeni":               "circulă până la Smeeni",
	"circulapanalaVaduSoresti":          "circulă până la Vadul Sorești",
	"circulaprin Bragareasa":            "circulă prin Brăgăreasa",
	"circulaprinBragareasa":              "circulă prin Brăgăreasa",
	"CirculapanalaSg.lonelStefan":       "circulă până la Sg. Ionel Ștefan",
	"CirculaprinSuchea":                 "circulă prin Suchea",
	"sensulduscirculaprin M.Banului":    "sensul dus circulă prin M. Banului",
	"sensulintors circulprinM.Banului":  "sensul întors circulă prin M. Banului",
}

# Trip-level overrides applied after schedule parsing, keyed by route_number.
# Each entry is a list of {field: value} dicts.  Keys 'way_departure' and
# 'way_arrival' are discriminators used to locate the matching trip; all other
# keys are applied as field overrides.  'way_arrival' is optional and only
# needed when multiple trips share the same 'way_departure'.
ROUTE_SCHEDULE_CORRECTIONS: dict[str, list[dict]] = {
	# Route 007: 17:00 departure is school-days-only but OCR annotation was
	# not captured for that trip.
	"007": [
		{"way_departure": "17:00", "only_for_school_days": True},
	],
	# Route 015: last trip (17:00→18:35) is Sunday-only; OCR missed the
	# "7" day annotation.  Use way_arrival to distinguish from the Mon-Sat
	# 17:00→18:20 trip.
	"015": [
		{"way_departure": "17:00", "way_arrival": "18:35", "days_of_operation": [7]},
	],
	# Route 028: OCR missed the 16:30 departure row entirely (the row does
	# not appear anywhere in the OCR output).  Insert it before the 17:00 trip.
	"028": [
		{
			"action": "insert",
			"way_departure": "16:30",
			"way_arrival": "17:40",
			"back_departure": "18:30",
			"back_arrival": "19:40",
			"days_of_operation": [1, 2, 3, 4, 5, 6, 7],
			"only_for_school_days": False,
			"notes": None,
			"insert_before": "17:00",
		},
	],
}
