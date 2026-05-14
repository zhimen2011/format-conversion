"""Format normalizer: converts AIRPORT1/RWYS → AIRPORT2/RWYU + #INT.

Handles the DATIS-style .rwy format where intersection departures are listed as
separate RWYS entries (e.g. 'A2-15') and normalizes them into the Boeing STAS
convention with #INT lines under the parent runway.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from models import Airport, Intersection, Runway

# Regex: intersection-style designator like "A2-15", "C11-33", "E10-34R"
_RE_INTERSECTION_DESIGNATOR = re.compile(
    r"^([A-Za-z]+\d*)\s*[-–—/]\s*(\d{1,2}[LRC]?)$"
)

# Known RWYU header params.
_KNOWN_RWYU_PARAMS: dict[tuple[str, str], dict] = {
    ("ZGSZ", "15"):  {"taxiway_entry_angle": 90, "missed_approach_gradient": 2.50, "runway_width": 45},
    ("ZGSZ", "16L"): {"taxiway_entry_angle": 90, "missed_approach_gradient": 2.50, "runway_width": 60},
    ("ZGSZ", "16R"): {"taxiway_entry_angle": 90, "missed_approach_gradient": 2.50, "runway_width": 45},
    ("ZGSZ", "33"):  {"taxiway_entry_angle": 90, "missed_approach_gradient": 2.50, "runway_width": 45},
    ("ZGSZ", "34L"): {"taxiway_entry_angle": 90, "missed_approach_gradient": 2.50, "runway_width": 45},
    ("ZGSZ", "34R"): {"taxiway_entry_angle": 90, "missed_approach_gradient": 2.50, "runway_width": 60},
}

# Country full-name → 3-letter code for city field normalization.
# e.g. "TIANJIN/CHINA" → "TIANJIN,CHN"
_COUNTRY_NAME_TO_CODE: dict[str, str] = {
    "CHINA": "CHN",
    "JAPAN": "JPN",
    "KOREA": "KOR",
    "THAILAND": "THA",
    "VIETNAM": "VNM",
    "MALAYSIA": "MYS",
    "SINGAPORE": "SGP",
    "INDONESIA": "IDN",
    "PHILIPPINES": "PHL",
    "MYANMAR": "MMR",
    "INDIA": "IND",
    "RUSSIA": "RUS",
    "UNITED STATES": "USA",
    "UNITED KINGDOM": "GBR",
    "FRANCE": "FRA",
    "GERMANY": "DEU",
    "AUSTRALIA": "AUS",
    "CANADA": "CAN",
}

# Legacy: specific city overrides that can't be handled by the generic rule.
# New airports should normally rely on _COUNTRY_NAME_TO_CODE instead.
_CITY_OVERRIDES: dict[str, str] = {
    "SHENZHEN/CHINA": "SHENZHEN,CHN",
}

def _normalize_city(city: str) -> str:
    """Normalize a city field from AIRPORT1 style to standard AIRPORT2 style.

    Rules (applied in order):
      1. Exact-match override from _CITY_OVERRIDES.
      2. If city contains ``/COUNTRY_NAME`` and the country name is known,
         convert to ``,CODE`` — e.g. ``TIANJIN/CHINA`` → ``TIANJIN,CHN``.
      3. Otherwise return unchanged.
    """
    if city in _CITY_OVERRIDES:
        return _CITY_OVERRIDES[city]
    if "/" in city:
        parts = city.rsplit("/", 1)
        if len(parts) == 2 and parts[1] in _COUNTRY_NAME_TO_CODE:
            return f"{parts[0]},{_COUNTRY_NAME_TO_CODE[parts[1]]}"
    return city


# --- IATA code loading ---

_IATA_CODES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iata_codes.json")

def _load_iata_lookup() -> dict[str, str]:
    """Load ICAO→IATA mapping from the external JSON file."""
    try:
        with open(_IATA_CODES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Strip metadata keys (anything starting with underscore)
    return {k: v for k, v in raw.items() if not k.startswith("_")}

_DEFAULT_EOP = "*** NO EMERGENCY TURN ***"


def _make_eop(text: str = _DEFAULT_EOP, date_str: str = "") -> str:
    """Build a 128-char engine-out procedure string with optional right-justified date."""
    if date_str:
        inner = f"{text}{' ' * (128 - len(text) - len(date_str))}{date_str}"
    else:
        inner = f"{text:<128}"
    return inner[:128]


def _detect_intersection_entries(runways: list[Runway]) -> tuple[list[Runway], dict[str, list[dict]]]:
    """Separate intersection-style runways from main runways.

    Returns:
        (main_runways, {parent_dsg: [{"name": ..., "offset": ..., "tora": ...}, ...]})
    """
    main: list[Runway] = []
    intersections: dict[str, list[dict]] = {}
    main_tora: dict[str, float] = {}

    for rw in runways:
        if _RE_INTERSECTION_DESIGNATOR.match(rw.designator):
            continue
        main.append(rw)
        main_tora[rw.designator] = rw.tora

    for rw in runways:
        m = _RE_INTERSECTION_DESIGNATOR.match(rw.designator)
        if not m:
            continue
        its_name = m.group(1)
        parent_dsg = m.group(2)

        full_tora = main_tora.get(parent_dsg, rw.lda if rw.lda > 0 else rw.tora + 100)
        offset = max(full_tora - rw.tora, 0.0)

        intersections.setdefault(parent_dsg, []).append({
            "name": its_name,
            "offset": offset,
            "tora": rw.tora,
            "lineup_angle": rw.taxiway_entry_angle if rw.taxiway_entry_angle else 90.0,
            "tod_slope": rw.tod_slope,
            "asd_slope": rw.asd_slope,
        })

    return main, intersections


def _auto_correct_slopes(rw: Runway) -> list[str]:
    """Auto-correct a slope value when two are equal and non-zero, but the third is 0.0.

    Common data-entry error: ``tod=0.03, asd=0.03, ldg=0.0`` where the ``0.0``
    should have been ``0.03``.  Only triggers on the precise pattern
    "two equal > 0, one == 0.0" to avoid false positives.

    Returns a list of warning messages (empty if nothing was corrected).
    """
    warnings: list[str] = []
    slopes = [
        ("tod_slope", rw.tod_slope),
        ("asd_slope", rw.asd_slope),
        ("landing_slope", rw.landing_slope),
    ]
    non_zero = [(name, val) for name, val in slopes if val != 0.0]
    zero = [(name, val) for name, val in slopes if val == 0.0]

    if len(non_zero) == 2 and len(zero) == 1:
        v0, v1 = non_zero[0][1], non_zero[1][1]
        if v0 == v1:
            zero_name = zero[0][0]
            setattr(rw, zero_name, v0)
            warnings.append(
                f"[WARN] {rw.designator}: {zero_name} was 0.0, "
                f"auto-corrected to {v0} (matched TOD/ASD slope)"
            )
    return warnings


def _merge_intersections_by_offset(entries: list[dict]) -> list[dict]:
    """Merge intersection entries that share the same offset.

    E.g. 'A2' (offset 125) + 'C2' (offset 125) → 'A2-C2' (offset 125).
    """
    by_offset: dict[float, list[dict]] = {}
    for e in entries:
        key = round(e["offset"], 1)
        by_offset.setdefault(key, []).append(e)

    merged = []
    for offset, group in by_offset.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            # Sort by name for consistent ordering
            group.sort(key=lambda x: x["name"])
            combined_name = "-".join(g["name"] for g in group)
            first = group[0].copy()
            first["name"] = combined_name
            merged.append(first)
    return merged


# Extract date from "*** EFF DATE *** YYYY.MM.DD" style annotations
_RE_EFF_DATE = re.compile(r"\*{1,3}\s*EFF\s+DATE\s*\*{0,3}\s*(\d{4})\.(\d{2})\.(\d{2})", re.IGNORECASE)

_MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _convert_eff_date(date_str: str) -> str:
    """Convert '2025.11.27' → '27 NOV 2025'."""
    parts = date_str.strip().split(".")
    if len(parts) != 3:
        return date_str
    y, m, d = parts
    month_idx = int(m) - 1
    if 0 <= month_idx < 12:
        return f"{int(d):02d} {_MONTH_NAMES[month_idx]} {y}"
    return date_str


def _normalize_comments(comments: list[str]) -> tuple[list[str], str | None]:
    """Strip leading 'H ' prefixes and extract EFF DATE annotation.

    Returns (cleaned_comments, eff_date_string_or_None).
    """
    result = []
    eff_date = None
    for c in comments:
        c = c.strip()
        while c.startswith("H ") or c.startswith("H\t"):
            c = c[2:].strip()
        m = _RE_EFF_DATE.search(c)
        if m:
            eff_date = _convert_eff_date(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
            continue
        result.append(c)
    return result, eff_date


def normalize_airport(
    airport: Airport,
    rwyu_params_override: Optional[dict[str, dict]] = None,
    eop_override: Optional[dict[str, str]] = None,
    iata_code: Optional[str] = None,
) -> Airport:
    """Normalize an Airport to standard STAS format.

    - Detects intersection-format runways (e.g. 'A2-15') and converts to #INT lines
    - Merges intersection entries that share the same offset (e.g. A2 + C2 → A2-C2)
    - Supplies missing RWYU header params from known defaults
    - Adds engine-out procedure lines when missing
    - Normalizes H-comment format and city names
    """
    icao = airport.icao_code
    known = _KNOWN_RWYU_PARAMS

    # ---- city normalization ----
    airport.city = _normalize_city(airport.city)

    # ---- separate main runways from intersection entries ----
    main_runways, intersections = _detect_intersection_entries(airport.runways)

    normalized_runways: list[Runway] = []
    for rw in main_runways:
        # --- fill missing RWYU header params ---
        key = (icao, rw.designator)
        defaults = known.get(key, {"taxiway_entry_angle": 90.0, "missed_approach_gradient": 2.50, "runway_width": 45.0})

        if rwyu_params_override and rw.designator in rwyu_params_override:
            defaults.update(rwyu_params_override[rw.designator])

        if rw.taxiway_entry_angle is None:
            rw.taxiway_entry_angle = defaults.get("taxiway_entry_angle", 90.0)
        if rw.missed_approach_gradient is None:
            rw.missed_approach_gradient = defaults.get("missed_approach_gradient", 2.50)
        if rw.runway_width is None:
            rw.runway_width = defaults.get("runway_width", 45.0)

        # --- auto-correct slope data-entry errors ---
        for msg in _auto_correct_slopes(rw):
            print(msg)

        # --- normalize comments (extract EFF DATE if present) ---
        rw_comments, eff_date = _normalize_comments(rw.comments)
        rw.comments = rw_comments

        # --- fill missing EOP (EFF DATE from comments overridden by eop_override) ---
        if not rw.engine_out_procedure:
            date_str = eff_date or ""
            if eop_override and rw.designator in eop_override:
                date_str = eop_override[rw.designator]
            rw.engine_out_procedure = _make_eop(date_str=date_str)

        # --- default comment when no special procedure ---
        if rw.engine_out_procedure.startswith(_DEFAULT_EOP):
            rw.comments = ["Straight on extended RWY centerline."]

        # --- attach intersections (merged by offset) ---
        if rw.designator in intersections:
            merged = _merge_intersections_by_offset(intersections[rw.designator])
            # sort by offset for consistent output
            merged.sort(key=lambda x: x["offset"])
            existing_names = {i.name for i in rw.intersections}
            for its_data in merged:
                if its_data["name"] not in existing_names:
                    rw.intersections.append(Intersection(
                        name=its_data["name"],
                        offset=its_data["offset"],
                        lineup_angle=its_data["lineup_angle"],
                        tod_slope=its_data["tod_slope"],
                        asd_slope=its_data["asd_slope"],
                    ))

        normalized_runways.append(rw)

    airport.runways = normalized_runways
    airport.comments, _ = _normalize_comments(airport.comments)

    # Auto-set IATA code if missing (loaded from external JSON)
    if not airport.iata_code:
        iata_lookup = _load_iata_lookup()
        if icao in iata_lookup:
            airport.iata_code = iata_lookup[icao]
    if iata_code:
        airport.iata_code = iata_code

    return airport


# ---------------------------------------------------------------------------
# convenience: parse + normalize + format in one call
# ---------------------------------------------------------------------------

def convert_rwy_file(
    input_path: str,
    output_path: str,
    iata_code: Optional[str] = None,
) -> Airport:
    """Read a .rwy file, normalize it, and write a standard .stx file."""
    from parser import parse_single_airport
    from formatter import format_airport_to_file

    airport = parse_single_airport(input_path)
    airport = normalize_airport(airport, iata_code=iata_code)
    format_airport_to_file(airport, output_path)
    return airport
