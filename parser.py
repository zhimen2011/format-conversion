"""Parser for STAS .rwy files — AIRPORT1/2 + RWYS/RWYU/RWYT/RWYV formats.

Reads a .rwy file and produces populated Airport / Runway / Obstacle /
Intersection model instances.
"""

from __future__ import annotations

import re

from models import (
    Airport,
    Intersection,
    Obstacle,
    Runway,
    RunwayFlag,
    SurfaceType,
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Extract a single-quoted string; two consecutive '' inside = escaped quote
_RE_QUOTED = re.compile(r"'((?:[^']|'')*)'")

# Detect runway-format header:  RWYS | RWYU | RWYT | RWYV
_RE_RWY_HEADER = re.compile(r"^(RWY[SUTV])\b\s*(.*)$")

# Detect intersection line
_RE_INT = re.compile(r"^#INT\b")

# Detect H-comment line (H in column 1, followed by space or content)
_RE_COMMENT = re.compile(r"^H(?:\s|$)")

# Blank line, whitespace-only, or file-level # comment (but NOT #INT)
_RE_SKIP = re.compile(r"^\s*(?:#(?!INT\b)|$)")

# Detect "no emergency turn" inside an engine-out procedure string
_RE_NO_EMERG_TURN = re.compile(r"\*{3}\s*NO\s+EMERGENCY\s+TURN\s*\*{3}")

# Obstacle line: three whitespace-separated numbers (height distance lateral_offset)
_RE_OBSTACLE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$")

# Turn-data line: 7 whitespace-separated numbers (FPTD 1-7)
_RE_TURN_DATA = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:\s+|$)){7}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_quoted(s: str) -> tuple[str, int]:
    """Extract the first single-quoted string from *s*.  Returns (value, span_end)
    where *value* has escaped '' resolved to ', and *span_end* is the index
    after the closing quote.  Raises ValueError if no quoted string found.
    """
    m = _RE_QUOTED.search(s)
    if not m:
        raise ValueError(f"No quoted string found in: {s!r}")
    value = m.group(1).replace("''", "'")
    return value, m.end()


def _extract_all_quoted(s: str) -> list[str]:
    """Return every single-quoted value in *s*, with '' → ' resolved."""
    return [m.group(1).replace("''", "'") for m in _RE_QUOTED.finditer(s)]


def _parse_number(s: str) -> float:
    """Robust string → float; returns 0.0 for empty / unparseable input."""
    s = s.strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Airport line parsers
# ---------------------------------------------------------------------------

def _parse_airport2(line: str) -> dict:
    """Parse AIRPORT2 fixed-column line → dict of Airport constructor kwargs."""
    # Column numbering from the spec is 1-based; Python slices are 0-based.
    return {
        "icao_code": line[0:4].strip(),
        "name": line[6:24].strip(),
        "city": line[26:44].strip(),
        "height_units": line[46],
        "distance_units": line[47],
        "height_ref": line[48:50],
        "distance_ref": line[50:52],
        "elevation": _parse_number(line[54:61]),
        "iata_code": line[62:].strip() or None,
    }


def _parse_airport1(line: str, elevation_line: str | None = None) -> dict:
    """Parse AIRPORT1 quote-delimited line → dict of Airport constructor kwargs.

    The spec lists: code, name, city, 6-char obs-ref-string.  Elevation may be
    present as an additional field (quoted or bare).  The 6-char reference
    string is decomposed into: height_units, distance_units, height_ref,
    distance_ref.
    """
    values = _extract_all_quoted(line)
    if len(values) < 4:
        raise ValueError(f"AIRPORT1 line has {len(values)} quoted fields, need ≥4: {line!r}")

    icao_code = values[0]
    name = values[1]
    city = values[2]
    ref_str = values[3]  # e.g. 'FMLOLO'

    # Decompose 6-char reference string
    if len(ref_str) != 6:
        raise ValueError(f"Obstacle reference string must be 6 chars, got {ref_str!r}")
    height_units = ref_str[0]  # F or M
    distance_units = ref_str[1]  # F or M
    height_ref = ref_str[2:4]  # BR, LO, MS
    distance_ref = ref_str[4:6]  # BR, LO

    # Elevation: if there's a 5th quoted field use it, else try bare trailing number
    elevation = 0.0
    iata_code: str | None = None
    if len(values) >= 5:
        elevation = _parse_number(values[4])
        if len(values) >= 6:
            iata_code = values[5] or None
    else:
        # No 5th quoted field — scan for a bare trailing number after the last quote
        last_quote_end = 0
        for m in _RE_QUOTED.finditer(line):
            last_quote_end = m.end()
        if last_quote_end > 0:
            remainder = line[last_quote_end:].strip()
            if remainder:
                # e.g. "14" or "14 SZX"
                parts = remainder.split()
                elevation = _parse_number(parts[0])
                if len(parts) >= 2:
                    iata_code = parts[1] or None

    return {
        "icao_code": icao_code,
        "name": name,
        "city": city,
        "height_units": height_units,
        "distance_units": distance_units,
        "height_ref": height_ref,
        "distance_ref": distance_ref,
        "elevation": elevation,
        "iata_code": iata_code,
    }


# ---------------------------------------------------------------------------
# Runway header line parser
# ---------------------------------------------------------------------------

def _parse_runway_header(line: str) -> tuple[str, dict]:
    """Parse a RWY* header line → (format_type, kwargs for Runway).

    format_type is one of 'RWYS', 'RWYU', 'RWYT', 'RWYV'.

    Optional positional parameters after the format keyword:
        taxiway_entry_angle  missed_approach_gradient  runway_width  magnetic_heading
    Each later parameter requires all earlier ones to be present.
    """
    m = _RE_RWY_HEADER.match(line)
    if not m:
        raise ValueError(f"Not a runway header line: {line!r}")
    fmt = m.group(1)
    rest = m.group(2).strip()

    kwargs: dict = {}
    if rest:
        parts = rest.split()
        names = ["taxiway_entry_angle", "missed_approach_gradient", "runway_width", "magnetic_heading"]
        for i, part in enumerate(parts):
            if i < len(names):
                kwargs[names[i]] = _parse_number(part)

    return fmt, kwargs


# ---------------------------------------------------------------------------
# Runway data line parser
# ---------------------------------------------------------------------------

def _parse_runway_data(line: str) -> dict:
    """Parse a runway data line (RWY* line 2) → dict of Runway constructor kwargs.

    Format: '<designator>'  flag  TORA  TODA/CWY  ASDA/SWY  LDA
            tod_slope  asd_slope  landing_slope  num_obstacles  surface_type
    """
    # Extract quoted designator first
    designator, end = _extract_quoted(line)
    # Remaining fields are space-delimited numbers
    rest = line[end:].split()
    if len(rest) < 9:
        raise ValueError(f"Runway data line has {len(rest)} fields after designator, need ≥9: {line!r}")

    flag_val = int(rest[0])

    return {
        "designator": designator,
        "runway_flag": RunwayFlag(flag_val),
        "tora": _parse_number(rest[1]),
        "toda_or_clearway": _parse_number(rest[2]),
        "asda_or_stopway": _parse_number(rest[3]),
        "lda": _parse_number(rest[4]),
        "tod_slope": _parse_number(rest[5]),
        "asd_slope": _parse_number(rest[6]),
        "landing_slope": _parse_number(rest[7]),
        "surface_type": SurfaceType(int(rest[9])) if len(rest) > 9 else SurfaceType.NORMAL,
        "_num_obstacles": int(rest[8]),  # transient — not a Runway field
    }


# ---------------------------------------------------------------------------
# Top-level file parser
# ---------------------------------------------------------------------------

def parse_rwy_file(filepath: str) -> list[Airport]:
    """Parse a .rwy file and return a list of Airport instances.

    A single file may contain multiple AIRPORT blocks.
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.readlines()

    # Strip trailing whitespace (but keep leading spaces for column-based parsing)
    lines = [rl.rstrip("\n\r") for rl in raw_lines]

    airports: list[Airport] = []
    idx = 0
    n = len(lines)

    while idx < n:
        line = lines[idx]

        # Skip blank / file-comment lines between blocks
        if _RE_SKIP.match(line):
            idx += 1
            continue

        # --- Airport header -------------------------------------------------
        if line.startswith("AIRPORT1"):
            idx, airport = _parse_airport_block(lines, idx, fmt="AIRPORT1")
        elif line.startswith("AIRPORT2"):
            idx, airport = _parse_airport_block(lines, idx, fmt="AIRPORT2")
        else:
            idx += 1
            continue

        if airport is not None:
            airports.append(airport)

    return airports


# ---------------------------------------------------------------------------
# Airport block parser
# ---------------------------------------------------------------------------

def _parse_airport_block(
    lines: list[str], start_idx: int, fmt: str
) -> tuple[int, Airport | None]:
    """Parse one AIRPORT1/2 block starting at *start_idx*.

    Returns (next_line_index, Airport_instance_or_None).
    """
    idx = start_idx
    n = len(lines)

    # Consume the AIRPORT1 / AIRPORT2 header line
    idx += 1
    if idx >= n:
        return idx, None

    # Find the airport data line (skip blank / # comment lines)
    while idx < n and _RE_SKIP.match(lines[idx]):
        idx += 1
    if idx >= n:
        return idx, None

    # Parse airport data line
    airport_line = lines[idx]
    if fmt == "AIRPORT2":
        airport_kwargs = _parse_airport2(airport_line)
    else:
        airport_kwargs = _parse_airport1(airport_line)
    idx += 1

    # Airport-level H comments (between airport line and first runway)
    airport_comments: list[str] = []
    while idx < n:
        line = lines[idx]
        if _RE_COMMENT.match(line):
            airport_comments.append(line[1:].strip())
            idx += 1
        elif _RE_SKIP.match(line):
            idx += 1
        else:
            break

    airport_kwargs["comments"] = airport_comments
    airport = Airport(**airport_kwargs)

    # --- Parse runway blocks until next AIRPORT or EOF ----------------------
    while idx < n:
        line = lines[idx]

        if line.startswith("AIRPORT1") or line.startswith("AIRPORT2"):
            break  # next airport block

        if _RE_SKIP.match(line):
            idx += 1
            continue

        m = _RE_RWY_HEADER.match(line)
        if not m:
            idx += 1
            continue

        rwy_fmt = m.group(1)  # RWYS, RWYU, RWYT, RWYV
        idx, runway = _parse_runway_block(lines, idx, rwy_fmt)
        if runway is not None:
            airport.runways.append(runway)

    return idx, airport


# ---------------------------------------------------------------------------
# Runway block parser
# ---------------------------------------------------------------------------

def _parse_runway_block(
    lines: list[str], start_idx: int, fmt: str
) -> tuple[int, Runway | None]:
    """Parse one RWY* block starting at *start_idx*.

    Returns (next_line_index, Runway_instance_or_None).
    """
    idx = start_idx
    n = len(lines)

    # --- Header line --------------------------------------------------------
    header_fmt, header_kwargs = _parse_runway_header(lines[idx])
    idx += 1

    # Skip blanks
    while idx < n and _RE_SKIP.match(lines[idx]):
        idx += 1
    if idx >= n:
        return idx, None

    # --- Data line ----------------------------------------------------------
    data_kwargs = _parse_runway_data(lines[idx])
    num_obstacles = data_kwargs.pop("_num_obstacles")
    idx += 1

    # --- Obstacle lines -----------------------------------------------------
    obstacles: list[Obstacle] = []
    for _ in range(num_obstacles):
        if idx >= n:
            break
        line = lines[idx]
        m = _RE_OBSTACLE.match(line)
        if m:
            obstacles.append(Obstacle(
                height=_parse_number(m.group(1)),
                distance=_parse_number(m.group(2)),
                lateral_offset=_parse_number(m.group(3)),
            ))
            idx += 1
        elif _RE_SKIP.match(line):
            idx += 1  # skip blanks inside obstacle list
        else:
            # Non-matching line — obstacle count mismatch; stop gracefully
            break

    # --- Turn data line (RWYT / RWYV only) ----------------------------------
    turn_data: list[float] = []
    if fmt in ("RWYT", "RWYV"):
        if idx < n:
            line = lines[idx]
            parts = line.split()
            if len(parts) >= 7 and all(_is_numberlike(p) for p in parts[:7]):
                turn_data = [_parse_number(p) for p in parts[:7]]
                idx += 1

    # --- Engine-out procedure (quoted line) ---------------------------------
    engine_out_procedure = ""
    emergency_turn_note = None
    if idx < n:
        line = lines[idx]
        try:
            eop, _ = _extract_quoted(line)
            engine_out_procedure = eop
            if _RE_NO_EMERG_TURN.search(eop):
                emergency_turn_note = eop
            idx += 1
        except ValueError:
            # Not a quoted line — maybe an empty procedure or missing
            pass

    # --- H comment lines (runway comments) ----------------------------------
    rwy_comments: list[str] = []
    while idx < n:
        line = lines[idx]
        if _RE_COMMENT.match(line):
            rwy_comments.append(line[1:].strip())
            idx += 1
        elif _RE_SKIP.match(line):
            idx += 1
        else:
            break

    # --- #INT intersection lines --------------------------------------------
    intersections: list[Intersection] = []
    while idx < n:
        line = lines[idx]
        if _RE_INT.match(line):
            intersections.append(_parse_intersection(line))
            idx += 1
        elif _RE_SKIP.match(line):
            idx += 1
        else:
            break

    runway = Runway(
        **header_kwargs,
        **data_kwargs,
        obstacles=obstacles,
        engine_out_procedure=engine_out_procedure,
        emergency_turn_note=emergency_turn_note,
        comments=rwy_comments,
        intersections=intersections,
    )

    # Attach turn_data as an attribute if present (not in the base model)
    if turn_data:
        runway.__dict__["turn_data"] = turn_data

    return idx, runway


# ---------------------------------------------------------------------------
# Intersection line parser
# ---------------------------------------------------------------------------

def _parse_intersection(line: str) -> Intersection:
    """Parse a #INT line → Intersection instance.

    Format:  #INT '<name>'  offset  lineup_angle  tod_slope  asd_slope
    """
    # Strip the #INT prefix
    rest = line[4:].strip()
    name, end = _extract_quoted(rest)
    parts = rest[end:].split()
    return Intersection(
        name=name,
        offset=_parse_number(parts[0]) if len(parts) > 0 else 0.0,
        lineup_angle=_parse_number(parts[1]) if len(parts) > 1 else 0.0,
        tod_slope=_parse_number(parts[2]) if len(parts) > 2 else 0.0,
        asd_slope=_parse_number(parts[3]) if len(parts) > 3 else 0.0,
    )


def _is_numberlike(s: str) -> bool:
    """True if *s* looks like a number (int or float, possibly negative)."""
    try:
        float(s)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Convenience entry-point (single airport)
# ---------------------------------------------------------------------------

def parse_single_airport(filepath: str) -> Airport:
    """Parse a .rwy file expected to contain exactly one airport."""
    airports = parse_rwy_file(filepath)
    if not airports:
        raise ValueError(f"No airport found in {filepath}")
    return airports[0]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else r"E:\TEST\format conversion\ZGSZ airport1.rwy"
    airports = parse_rwy_file(path)

    for ap in airports:
        print(f"=== {ap.icao_code}  {ap.name}  ({ap.city}) ===")
        print(f"    Units: H={ap.height_units} D={ap.distance_units}  "
              f"HRef={ap.height_ref} DRef={ap.distance_ref}  Elev={ap.elevation}")
        if ap.iata_code:
            print(f"    IATA: {ap.iata_code}")
        if ap.comments:
            print(f"    Airport comments: {ap.comments}")

        for rw in ap.runways:
            print(f"\n  -- RWY {rw.designator} "
                  f"(flag={rw.runway_flag.name}) "
                  f"TORA={rw.tora} TODA/CWY={rw.toda_or_clearway} "
                  f"ASDA/SWY={rw.asda_or_stopway} LDA={rw.lda}")
            print(f"     Slopes: TOD={rw.tod_slope}% ASD={rw.asd_slope}% "
                  f"LDG={rw.landing_slope}%  Surface={rw.surface_type.name}")
            if rw.taxiway_entry_angle is not None:
                print(f"     Header: angle={rw.taxiway_entry_angle}  "
                      f"grad={rw.missed_approach_gradient}  "
                      f"width={rw.runway_width}  mag={rw.magnetic_heading}")

            for i, obs in enumerate(rw.obstacles, 1):
                print(f"     Obstacle {i}: h={obs.height}  d={obs.distance}  "
                      f"lat={obs.lateral_offset}")

            print(f"     EOP: {rw.engine_out_procedure[:80]}...")
            if rw.emergency_turn_note:
                print(f"     EmergTurn: {rw.emergency_turn_note}")
            for c in rw.comments:
                print(f"     H: {c}")
            for its in rw.intersections:
                print(f"     #INT {its.name}  offset={its.offset}  "
                      f"angle={its.lineup_angle}  "
                      f"TOD_slope={its.tod_slope}  ASD_slope={its.asd_slope}")
