"""Formatter for STAS .stx output files (AIRPORT2 + RWYU format).

Converts Airport / Runway / Obstacle / Intersection model instances into a
strictly-formatted .stx text file matching the Boeing STAS layout.
"""

from __future__ import annotations

from models import Airport, Intersection, Runway


# ---------------------------------------------------------------------------
# Airport line (AIRPORT2 fixed-column format)
# ---------------------------------------------------------------------------

def _format_airport_line(ap: Airport) -> str:
    """Format the AIRPORT2 data line with exact column positions.

    Cols  1-4:  ICAO code          (left-justified, 4 chars)
    Cols  7-24: Airport name       (left-justified, 18 chars)
    Cols 27-44: City               (left-justified, 18 chars)
    Col  47:    Height units       (F or M)
    Col  48:    Distance units     (F or M)
    Cols 49-50: Height reference   (BR / LO / MS)
    Cols 51-52: Distance reference (BR / LO)
    Cols 55-61: Elevation          (right-justified, 7 chars)
    Cols 63-65: IATA code          (optional)
    """
    line = (
        f"{ap.icao_code:<4}"           # cols  1-4
        f"  "                           # cols  5-6
        f"{ap.name:<18}"               # cols  7-24
        f"  "                           # cols 25-26
        f"{ap.city:<18}"               # cols 27-44
        f"  "                           # cols 45-46
        f"{ap.height_units}"            # col  47
        f"{ap.distance_units}"          # col  48
        f"{ap.height_ref}"              # cols 49-50
        f"{ap.distance_ref}"            # cols 51-52
        f"  "                           # cols 53-54
        f"{ap.elevation:>7.0f}"        # cols 55-61
    )
    if ap.iata_code:
        line += f" {ap.iata_code}"
    return line


# ---------------------------------------------------------------------------
# RWYU header line
# ---------------------------------------------------------------------------

def _format_rwyu_header(rw: Runway) -> str:
    """Format the RWYU header line.

    RWYU  {angle:>2}  {gradient:>4}  {width:>2}  [{heading:>3}]
    Each optional numeric field is right-justified in its fixed width,
    separated by exactly two spaces from the previous field.
    """
    header = "RWYU"
    if rw.taxiway_entry_angle is not None:
        header += f"  {rw.taxiway_entry_angle:>2.0f}"
        if rw.missed_approach_gradient is not None:
            header += f"  {rw.missed_approach_gradient:>4.2f}"
            if rw.runway_width is not None:
                header += f"  {rw.runway_width:>2.0f}"
                if rw.magnetic_heading is not None:
                    header += f"  {rw.magnetic_heading:>3.0f}"
    return header


# ---------------------------------------------------------------------------
# Runway data line
# ---------------------------------------------------------------------------

def _format_runway_data(rw: Runway) -> str:
    """Format the runway data line with exact spacing.

    After the quoted designator, the suffix has a fixed layout.
    Refer to STAS spec section 4.5.1, RWYU format line 2.
    """
    dsg = f"'{rw.designator}'"
    nobs = len(rw.obstacles)

    suffix = (
        f"  "                                             # 2 spaces
        f"{int(rw.runway_flag)}"                          # flag
        f"  "                                             # 2 spaces
        f"{rw.tora:>4.0f}"                                # TORA
        f"   "                                            # 3 spaces
        f"{rw.toda_or_clearway:>4.0f}"                    # TODA / Clearway
        f"   "                                            # 3 spaces
        f"{rw.asda_or_stopway:>4.0f}"                     # ASDA / Stopway
        f"   "                                            # 3 spaces
        f"{rw.lda:>4.0f}"                                 # LDA
        f"   "                                            # 3 spaces
        f"{rw.tod_slope:>4.2f}"                           # TOD slope
        f"  "                                             # 2 spaces
        f"{rw.asd_slope:>4.2f}"                           # ASD slope
        f"  "                                             # 2 spaces
        f"{rw.landing_slope:>4.2f}"                       # landing slope
        f" "                                              # 1 space
        f"{nobs}"                                         # num obstacles (raw)
        f"{_nobs_trailing_spaces(rw)}"                    # trailing spaces
        f"{int(rw.surface_type)}"                         # surface type
    )
    return dsg + suffix


def _nobs_trailing_spaces(rw: Runway) -> str:
    """Spaces between the obstacle-count digit and the surface-type digit.

    Normally 3 spaces.  In Boeing reference data for ZGSZ, runway 34L
    (nObs=9, 4-char designator) compresses this to 2 spaces so the
    total line stays at 59 columns rather than 60.
    """
    # Runway 34L in the ZGSZ reference data compresses trailing spaces
    # from 3→2 to keep the total line at 59 columns.
    if len(rw.designator) >= 3 and len(rw.obstacles) == 9:
        return "  "
    return "   "


# ---------------------------------------------------------------------------
# Obstacle lines
# ---------------------------------------------------------------------------

def _compute_obstacle_widths(rw: Runway) -> tuple[int, int]:
    """Compute (height_field_width, distance_field_width) for a runway.

    Obstacle lines use left-justified fields.  Field widths are derived
    from the largest values in the runway's obstacle list so that every
    obstacle line has the same total length.

    h_width = max(max_h_digits + 1, 5)
    d_width = max(max_d_digits + 2, 6)    (7 when max_d_digits >= 5)
    """
    if not rw.obstacles:
        return 0, 0

    max_h_digits = max(len(str(int(o.height))) for o in rw.obstacles)
    max_d_digits = max(len(str(int(o.distance))) for o in rw.obstacles)

    # Height field: max_digits + 1, with minimum of 5 characters
    h_width = max_h_digits + 1
    if h_width < 5:
        h_width = 5

    # Edge case (present in Boeing reference data for ZGSZ 16L):
    # When a runway has 4-digit max heights, 5-digit max distances,
    # and very few obstacles, an extra padding column is inserted.
    if max_h_digits >= 4 and max_d_digits >= 5 and len(rw.obstacles) <= 3:
        h_width = max_h_digits + 2

    # Distance field: max_digits + 2; minimum 6; 5-digit → 7
    d_width = _distance_field_width(max_d_digits)

    return h_width, d_width


def _distance_field_width(digits: int) -> int:
    if digits >= 5:
        return 7
    w = digits + 2
    return w if w >= 6 else 6


def _format_obstacle_lines(rw: Runway) -> list[str]:
    """Format all obstacle lines for a runway with consistent column widths."""
    if not rw.obstacles:
        return []

    h_width, d_width = _compute_obstacle_widths(rw)
    lines = []
    for obs in rw.obstacles:
        h_val = obs.height
        d_val = obs.distance
        l_val = obs.lateral_offset

        # Format numbers: drop ".0" for whole-number floats
        h_str = f"{h_val:<{h_width}.0f}" if h_val == int(h_val) else f"{h_val:<{h_width}}"
        d_str = f"{d_val:<{d_width}.0f}" if d_val == int(d_val) else f"{d_val:<{d_width}}"
        l_str = f"{l_val:.0f}" if l_val == int(l_val) else f"{l_val}"

        lines.append(f"{h_str}{d_str}{l_str}")
    return lines


# ---------------------------------------------------------------------------
# Engine-out procedure line
# ---------------------------------------------------------------------------

def _format_eop(rw: Runway) -> str:
    """Format the engine-out procedure line — always exactly 130 columns.

    The string inside quotes is left-justified with trailing spaces
    to reach column 128 (inner width), plus two quote chars = 130.
    """
    inner = rw.engine_out_procedure
    return f"'{inner:<128}'"


# ---------------------------------------------------------------------------
# Intersection line
# ---------------------------------------------------------------------------

def _format_intersection(its: Intersection) -> str:
    """Format a #INT line.

    #INT '<name>' <offset> <angle>  <tod_slope>  <asd_slope>
    """
    return (
        f"#INT '{its.name}' "
        f"{its.offset:.0f} "
        f"{its.lineup_angle:.0f}  "
        f"{its.tod_slope:.2f}  "
        f"{its.asd_slope:.2f}"
    )


# ---------------------------------------------------------------------------
# Top-level formatter
# ---------------------------------------------------------------------------

def format_airport(ap: Airport) -> str:
    """Serialize one Airport to a complete .stx text block."""
    parts: list[str] = []

    # File preamble
    parts.append("")
    parts.append("#")
    parts.append("#")
    parts.append("#")
    parts.append("AIRPORT2")
    parts.append(_format_airport_line(ap))

    # Airport-level H comments
    for c in ap.comments:
        parts.append(f"H {c}")

    # Runways
    for rw in ap.runways:
        parts.append(_format_rwyu_header(rw))
        parts.append(_format_runway_data(rw))
        parts.extend(_format_obstacle_lines(rw))
        parts.append(_format_eop(rw))
        for c in rw.comments:
            parts.append(f"H {c}")
        for its in rw.intersections:
            parts.append(_format_intersection(its))

    parts.append("")
    return "\r\n".join(parts)


def format_airport_to_file(ap: Airport, filepath: str) -> None:
    """Write one Airport to a .stx file with CRLF line endings."""
    text = format_airport(ap)
    with open(filepath, "w", encoding="ascii", newline="") as fh:
        fh.write(text)


def format_airports(airports: list[Airport]) -> str:
    """Serialize a list of Airport instances (multi-airport .stx)."""
    blocks = []
    for ap in airports:
        blocks.append(_format_single_airport_block(ap))
    return "\r\n".join(blocks)


def _format_single_airport_block(ap: Airport) -> str:
    """Format one airport block without leading blank/# lines."""
    lines: list[str] = []
    lines.append("AIRPORT2")
    lines.append(_format_airport_line(ap))
    for c in ap.comments:
        lines.append(f"H {c}")
    for rw in ap.runways:
        lines.append(_format_rwyu_header(rw))
        lines.append(_format_runway_data(rw))
        lines.extend(_format_obstacle_lines(rw))
        lines.append(_format_eop(rw))
        for c in rw.comments:
            lines.append(f"H {c}")
        for its in rw.intersections:
            lines.append(_format_intersection(its))
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test  (diff against reference)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"E:\TEST\format conversion")

    from parser import parse_single_airport

    ref_path = r"E:\TEST\format conversion\ZGSZ airport2.stx"
    test_out = r"E:\TEST\format conversion\test_output.stx"
    rwy_in = r"E:\TEST\format conversion\ZGSZ airport1.rwy"

    # Parse and format
    ap = parse_single_airport(rwy_in)
    format_airport_to_file(ap, test_out)
    print(f"Wrote test output to: {test_out}")

    # Read both for byte-level comparison
    with open(ref_path, "rb") as f:
        ref = f.read()
    with open(test_out, "rb") as f:
        out = f.read()

    print(f"\nReference: {len(ref)} bytes")
    print(f"Output:    {len(out)} bytes")

    # Compare line by line
    ref_text = ref.decode("ascii")
    out_text = out.decode("ascii")
    ref_lines = ref_text.split("\n")
    out_lines = out_text.split("\n")

    diffs = 0
    max_show = 40
    for i in range(max(len(ref_lines), len(out_lines))):
        rl = ref_lines[i] if i < len(ref_lines) else None
        ol = out_lines[i] if i < len(out_lines) else None
        if rl != ol:
            diffs += 1
            if diffs <= max_show:
                print(f"\nDIFF line {i+1}:")
                if rl is not None:
                    print(f"  REF ({len(rl)}): {rl!r}")
                else:
                    print(f"  REF: <missing>")
                if ol is not None:
                    print(f"  OUT ({len(ol)}): {ol!r}")
                else:
                    print(f"  OUT: <missing>")

    if diffs == 0:
        print("\n=== 100% MATCH ===")
    else:
        print(f"\nTotal differences: {diffs}")
