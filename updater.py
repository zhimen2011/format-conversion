"""Updater for STAS .stx files — local intersection-data injection.

Workflow:
  1. Extract intersection departure data from airport distance charts
     (PDF: pdfplumber table extraction  |  image: RapidOCR with row clustering).
  2. Read the existing .stx file, match runways, compute offsets.
  3. Inject #INT lines and save the updated .stx file.

Usage:
  python updater.py airport.stx chart.pdf
  python updater.py airport.stx chart.png -o updated.stx --dry-run
  python updater.py airport.stx --from-json extracted.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from typing import Optional

# Ensure local modules are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import Intersection, Runway  # noqa: E402
from parser import parse_single_airport  # noqa: E402
from formatter import format_airport_to_file  # noqa: E402


# ===================================================================
#  Utility helpers
# ===================================================================

# Chinese + English column-header keywords  (lowercased for matching)
_TORA_HEADERS = ["tora", "可用起飞滑跑距离", "起飞滑跑距离", "可用起飞滑跑", "todr",
                 "takeoff run available"]
_NOTE_HEADERS = ["备注", "notes", "remarks", "note", "说明", "remark"]
_RWY_HEADERS  = ["跑道", "runway", "rwy", "rwy id", "跑道号"]

# Regex: extract intersection name from a note like "由 A2-C2 进入" or "由A2进入"
# The separator class includes en-dash (U+2013), em-dash (U+2014),
# Chinese enumeration comma 、(U+3001) and fullwidth comma ，(U+FF0C) for OCR tolerance.
_RE_INTERSECTION_NOTE = re.compile(
    r"由\s*([A-Za-z0-9]+(?:[\s\-–—/、，]*[A-Za-z0-9]+)*)\s*进入"
)

# Fallback: bare intersection-name pattern (e.g. "A2-C2", "C11", "E10") without 由…进入
_RE_INTERSECTION_NAME = re.compile(
    r"([A-Za-z]+\d+(?:[\s\-–—/、，]+[A-Za-z]+\d+)*)"
)

# Regex: standalone runway designator (e.g. "15", "16L", "34R")
_RE_RWY_DESIGNATOR = re.compile(r"\b(0?[1-9]|[1-2]\d|3[0-6])[LRC]?\b")

# Regex: detect a potential TORA numeric value (integer or decimal, possibly
# followed by "m" or "M")
_RE_TORA_VALUE = re.compile(r"(\d{3,5}(?:\.\d+)?)\s*[mM]?")


def _clean_cell(text: str | None) -> str:
    """Normalise a table cell value."""
    if text is None:
        return ""
    return text.strip().replace("\n", " ").replace("\r", " ")


def _normalize_intersection_name(raw: str) -> str:
    """Normalize an extracted intersection name to canonical form.

    Replaces Chinese enumeration commas (、, ，) and other separator noise
    with hyphens, collapses runs, and strips edges.
    """
    # Replace Chinese / fullwidth punctuation with hyphen
    name = raw.strip()
    name = name.replace("、", "-").replace("，", "-")
    # Collapse separator runs (spaces, hyphens, dashes) into a single hyphen
    name = re.sub(r"[\s\-–—/]+", "-", name)
    return name.strip("-")


def _to_float(text: str) -> float | None:
    """Try to parse a string as float; strip common noise first."""
    t = text.strip().replace(",", "").replace("，", "").replace(" ", "")
    t = re.sub(r"[mM].*$", "", t)   # drop trailing unit
    try:
        return float(t)
    except ValueError:
        return None


def _contains_any(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(kw in low for kw in keywords)


def _find_column(header_row: list[str | None], keywords: list[str]) -> int | None:
    """Return the index of the first header cell matching any keyword."""
    for i, cell in enumerate(header_row):
        if cell and _contains_any(str(cell), keywords):
            return i
    return None


# ===================================================================
#  PDF extraction  (pdfplumber)
# ===================================================================

def _extract_from_pdf(pdf_path: str) -> list[dict]:
    """Extract intersection rows from a PDF using pdfplumber table parsing."""
    import pdfplumber

    results: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            # --- Strategy A: extract_tables (bordered / semi-bordered tables)
            tables = page.extract_tables()
            for table in tables:
                results.extend(_parse_table(table, page_num))

    # Deduplicate by (runway, intersection_name)
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for r in results:
        key = (r["runway"], r["intersection_name"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _parse_table(table: list[list[str | None]], page_num: int = 0) -> list[dict]:
    """Parse a single pdfplumber table for intersection departure rows."""
    if not table or len(table) < 2:
        return []

    # --- Identify header row -------------------------------------------------
    header = table[0]
    tora_col = _find_column(header, _TORA_HEADERS)
    note_col = _find_column(header, _NOTE_HEADERS)
    rwy_col  = _find_column(header, _RWY_HEADERS)

    if tora_col is None or note_col is None:
        return []

    # --- Scan data rows ------------------------------------------------------
    results: list[dict] = []
    for row_idx, row in enumerate(table[1:], 2):
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue

        # ---- extract intersection name (multi-strategy) --------------------
        intersection_name = ""
        note = _clean_cell(row[note_col]) if note_col < len(row) else ""

        # Strategy 1: standard "由…进入" pattern
        m = _RE_INTERSECTION_NOTE.search(note)
        if m:
            intersection_name = _normalize_intersection_name(m.group(1))

        # Strategy 2: bare intersection name in the note cell
        if not intersection_name:
            m = _RE_INTERSECTION_NAME.search(note)
            if m:
                intersection_name = _normalize_intersection_name(m.group(1))

        # Strategy 3: scan every cell for an intersection-name pattern
        if not intersection_name:
            for cell in row:
                text = _clean_cell(cell)
                m = _RE_INTERSECTION_NAME.search(text)
                if m:
                    intersection_name = _normalize_intersection_name(m.group(1))
                    break

        if not intersection_name:
            continue

        # ---- TORA -----------------------------------------------------------
        if tora_col >= len(row):
            continue
        tora = _to_float(str(row[tora_col] or ""))
        if tora is None or tora <= 0:
            continue

        # ---- runway (multi-strategy) ----------------------------------------
        runway = ""
        if rwy_col is not None and rwy_col < len(row):
            runway = _clean_cell(row[rwy_col])
        if not runway:
            runway = _infer_runway(note, row, header) or ""
        if not runway:
            continue

        results.append({
            "runway": runway,
            "intersection_name": intersection_name,
            "tora": tora,
        })

    return results


def _infer_runway(
    note: str,
    row: list[str | None],
    header: list[str | None],
) -> str | None:
    """Try to infer runway designator from note text or adjacent columns."""
    # 1. Look for a runway-like pattern in the note itself
    m = _RE_RWY_DESIGNATOR.search(note)
    if m:
        return m.group(0)

    # 2. Scan all cells in the row for a designator pattern
    for cell in row:
        text = _clean_cell(cell)
        m = _RE_RWY_DESIGNATOR.search(text)
        if m and not _contains_any(text, _TORA_HEADERS + _NOTE_HEADERS):
            return m.group(0)

    # 3. Scan for "跑道XX" or "RWY XX" prefixed pattern
    for cell in row:
        text = _clean_cell(cell)
        m = re.search(r"(?:跑道|RWY|rwy)\s*(\d{1,2}[LRC]?)", text)
        if m:
            return m.group(1)

    return None


# ===================================================================
#  Image extraction  (RapidOCR + row clustering)
# ===================================================================

def _extract_from_image(image_path: str) -> list[dict]:
    """Extract intersection rows from an image using RapidOCR + layout heuristics."""
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    ocr_result, _ = engine(image_path)

    if not ocr_result:
        print("[WARN] RapidOCR returned no text from the image.")
        return []

    # ---- Group OCR boxes into rows by y-coordinate --------------------------
    rows = _cluster_ocr_rows(ocr_result)
    if not rows:
        return []

    # ---- Parse the gridded rows as a table ----------------------------------
    return _parse_ocr_table(rows)


def _cluster_ocr_rows(
    ocr_result: list,
) -> list[list[tuple[str, float, float]]]:
    """Cluster OCR-detected text boxes into table rows.

    Each OCR result element is:  [bbox, text, confidence]
      bbox = [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]

    Returns a list of rows, each row a list of (text, x_center, y_center),
    sorted by increasing x.
    """
    if not ocr_result:
        return []

    # Normalise: extract (text, x_centre, y_centre, height) for each box
    items: list[tuple[str, float, float, float]] = []
    for box, text, conf in ocr_result:
        if not text or not text.strip():
            continue
        x_centre = (box[0][0] + box[2][0]) / 2
        y_centre = (box[0][1] + box[2][1]) / 2
        height   = abs(box[2][1] - box[0][1])
        items.append((text.strip(), x_centre, y_centre, height))

    if not items:
        return []

    # Sort by y_centre
    items.sort(key=lambda t: t[2])

    # Average character height for proximity threshold
    avg_h = sum(t[3] for t in items) / len(items)
    y_gap = avg_h * 0.6

    # Cluster into rows
    rows_raw: list[list[tuple[str, float, float, float]]] = []
    current = [items[0]]
    for item in items[1:]:
        if abs(item[2] - current[-1][2]) < y_gap:
            current.append(item)
        else:
            rows_raw.append(current)
            current = [item]
    rows_raw.append(current)

    # Sort cells within each row by x_centre, strip height
    rows: list[list[tuple[str, float, float]]] = []
    for row in rows_raw:
        row.sort(key=lambda t: t[1])  # by x_centre
        rows.append([(t[0], t[1], t[2]) for t in row])

    return rows


def _parse_ocr_table(
    rows: list[list[tuple[str, float, float]]],
) -> list[dict]:
    """Identify the header row in OCR output, then extract intersection rows."""
    if len(rows) < 2:
        return []

    # ---- Find the header row (contains TORA + 备注 keywords) ----------------
    header_idx = -1
    for i, row in enumerate(rows):
        row_text = " ".join(c[0] for c in row)
        if _contains_any(row_text, _TORA_HEADERS) and _contains_any(row_text, _NOTE_HEADERS):
            header_idx = i
            break

    if header_idx < 0:
        # Fallback: use the first row that looks like a header (has ≥3 cells)
        for i, row in enumerate(rows):
            if len(row) >= 3:
                header_idx = i
                break
    if header_idx < 0:
        return []

    header_row = rows[header_idx]
    header_texts = [c[0] for c in header_row]

    tora_col = _find_column(header_texts, _TORA_HEADERS)
    note_col = _find_column(header_texts, _NOTE_HEADERS)
    rwy_col  = _find_column(header_texts, _RWY_HEADERS)

    if tora_col is None:
        # Try to find TORA column by looking for numeric values in data rows
        tora_col = _guess_numeric_column(rows, header_idx, exclude={note_col})
    if tora_col is None or note_col is None:
        return []

    # ---- Extract data rows --------------------------------------------------
    results: list[dict] = []
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]

        # ---- extract intersection name (multi-strategy) --------------------
        intersection_name = ""
        note = row[note_col][0] if note_col < len(row) else ""

        # Strategy 1: standard "由…进入" pattern
        m = _RE_INTERSECTION_NOTE.search(note)
        if m:
            intersection_name = _normalize_intersection_name(m.group(1))

        # Strategy 2: bare intersection name in the note cell
        if not intersection_name:
            m = _RE_INTERSECTION_NAME.search(note)
            if m:
                intersection_name = _normalize_intersection_name(m.group(1))

        # Strategy 3: scan every cell for an intersection-name pattern
        if not intersection_name:
            for cell in row:
                m = _RE_INTERSECTION_NAME.search(cell[0])
                if m:
                    intersection_name = _normalize_intersection_name(m.group(1))
                    break

        if not intersection_name:
            continue

        # ---- TORA -----------------------------------------------------------
        tora_text = row[tora_col][0] if tora_col < len(row) else ""
        tora = _to_float(tora_text)
        if tora is None or tora <= 0:
            continue

        # ---- runway (multi-strategy) ----------------------------------------
        runway = ""
        if rwy_col is not None and rwy_col < len(row):
            runway = row[rwy_col][0]

        if not runway:
            # Scan the row for a bare runway designator
            for cell in row:
                m2 = _RE_RWY_DESIGNATOR.search(cell[0])
                if m2:
                    runway = m2.group(0)
                    break

        if not runway:
            # Scan for "跑道XX" or "RWY XX" prefixed pattern
            for cell in row:
                m2 = re.search(r"(?:跑道|RWY|rwy)\s*(\d{1,2}[LRC]?)", cell[0])
                if m2:
                    runway = m2.group(1)
                    break

        if not runway:
            continue

        results.append({
            "runway": runway,
            "intersection_name": intersection_name,
            "tora": tora,
        })

    # Deduplicate
    seen = set()
    unique = []
    for r in results:
        key = (r["runway"], r["intersection_name"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _guess_numeric_column(
    rows: list[list[tuple[str, float, float]]],
    header_idx: int,
    exclude: set[int] | None = None,
) -> int | None:
    """Heuristic: find the column whose data rows are predominantly numeric."""
    exclude = exclude or set()
    if header_idx + 1 >= len(rows):
        return None

    # Count numerics per column across the first ~10 data rows
    max_cols = max(len(r) for r in rows[header_idx + 1:])
    numeric_counts: dict[int, int] = defaultdict(int)
    total_counts: dict[int, int] = defaultdict(int)

    for row in rows[header_idx + 1 : header_idx + 11]:
        for col_idx, cell in enumerate(row):
            if col_idx in exclude:
                continue
            total_counts[col_idx] += 1
            if _to_float(cell[0]) is not None:
                numeric_counts[col_idx] += 1

    best_col = None
    best_ratio = 0.0
    for col_idx in total_counts:
        if total_counts[col_idx] >= 2:
            ratio = numeric_counts[col_idx] / total_counts[col_idx]
            if ratio > best_ratio:
                best_ratio = ratio
                best_col = col_idx

    return best_col if best_ratio >= 0.5 else None


# ===================================================================
#  Public extraction entry-point
# ===================================================================

def extract_intersections_from_chart(
    chart_path: str,
    dpi: int = 200,
) -> list[dict]:
    """Extract intersection departure data from an airport chart (PDF or image).

    Uses purely local, offline libraries:
      - PDF  →  pdfplumber table extraction
      - PNG / JPG →  RapidOCR with row-clustering heuristics

    Args:
        chart_path: Path to a ``.pdf`` or image file (``.png``, ``.jpg``, …).
        dpi: (Reserved for future use; currently unused.)

    Returns:
        ``[{"runway": "15", "intersection_name": "A3", "tora": 3100}, …]``
    """
    ext = os.path.splitext(chart_path)[1].lower()

    if ext == ".pdf":
        print(f"[EXTRACT] PDF mode — using pdfplumber on: {chart_path}")
        return _extract_from_pdf(chart_path)

    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        print(f"[EXTRACT] Image mode — using RapidOCR on: {chart_path}")
        return _extract_from_image(chart_path)

    raise ValueError(f"Unsupported file type: {ext}")


# ===================================================================
#  Validation & offset calculation
# ===================================================================

class TORAValidationError(Exception):
    """Intersection TORA is not valid relative to the full-runway TORA."""


def compute_offset(
    full_tora: float,
    intersection_tora: float,
    runway_id: str = "?",
    intersection_name: str = "?",
) -> float:
    """Validate intersection TORA and compute the offset from runway start.

    ``offset = full_runway_TORA − intersection_TORA``

    Raises:
        TORAValidationError: if *intersection_tora* > *full_tora*.
    """
    if intersection_tora > full_tora:
        raise TORAValidationError(
            f"RWY {runway_id} / {intersection_name}: "
            f"intersection TORA ({intersection_tora:.0f} m) exceeds "
            f"full-runway TORA ({full_tora:.0f} m).  Skipping."
        )
    if intersection_tora == full_tora:
        print(
            f"[INFO] RWY {runway_id} / {intersection_name}: "
            f"intersection TORA equals full TORA — this may be a full-runway "
            f"departure, not an intersection.  Verify the chart data."
        )
    return full_tora - intersection_tora


# ===================================================================
#  Upsert helpers
# ===================================================================

def _upsert_intersections(
    runway: Runway,
    new_entries: list[dict],
    full_tora: float,
) -> dict:
    """Add or update intersections on a *runway* object.

    Returns:
        ``{"added": n, "updated": n, "skipped": n, "errors": [...]}``
    """
    stats: dict = {"added": 0, "updated": 0, "skipped": 0, "errors": []}
    by_name: dict[str, Intersection] = {i.name: i for i in runway.intersections}

    for entry in new_entries:
        name = entry["intersection_name"]
        tora = entry["tora"]

        # --- validate -------------------------------------------------------
        try:
            offset = compute_offset(full_tora, tora, runway.designator, name)
        except TORAValidationError as exc:
            stats["skipped"] += 1
            stats["errors"].append(str(exc))
            print(f"[WARN] {exc}")
            continue

        # --- build intersection ---------------------------------------------
        its = Intersection(
            name=name,
            offset=offset,
            lineup_angle=90.0,
            tod_slope=0.0,
            asd_slope=0.0,
        )

        action = "Updated" if name in by_name else "Added"
        stats["updated" if name in by_name else "added"] += 1
        print(
            f"[INFO] {action} #INT '{name}' on RWY {runway.designator}: "
            f"offset = {full_tora:.0f} − {tora:.0f} = {offset:.0f} m"
        )
        by_name[name] = its

    runway.intersections = list(by_name.values())
    return stats


# ===================================================================
#  Main workflow
# ===================================================================

def update_stx_file(
    stx_path: str,
    chart_path: str | None = None,
    *,
    extracted_data: list[dict] | None = None,
    output_path: Optional[str] = None,
) -> str:
    """Read an .stx file, merge intersection data, write updated .stx.

    Intersection data can come from local chart extraction (*chart_path*)
    or be passed directly via *extracted_data* (offline / cached runs).

    Args:
        stx_path: Path to existing .stx file.
        chart_path: Path to airport chart (PDF / image).  May be ``None`` if
            *extracted_data* is supplied.
        extracted_data: Pre-extracted intersection list (bypasses chart parsing).
        output_path: Where to write the updated file (default: overwrite).

    Returns:
        Path to the updated .stx file.
    """
    if output_path is None:
        output_path = stx_path
    if extracted_data is None and chart_path is None:
        raise ValueError("Either chart_path or extracted_data must be provided.")

    # ---- Step 1 — Local extraction -----------------------------------------
    if extracted_data is not None:
        print(f"[STEP 1] Using supplied intersection data ({len(extracted_data)} entries)")
    else:
        print(f"[STEP 1] Extracting intersection data from: {chart_path}")
        extracted_data = extract_intersections_from_chart(chart_path)

    if not extracted_data:
        print("[STEP 1] No intersection departures found — nothing to update.")
        return output_path

    print(f"[STEP 1] {len(extracted_data)} intersection(s) extracted:")
    for e in extracted_data:
        print(f"         RWY {e['runway']:5s}  {e['intersection_name']:8s}  TORA={e['tora']:.0f} m")

    # ---- Step 2 — Parse .stx -----------------------------------------------
    print(f"\n[STEP 2] Reading existing .stx: {stx_path}")
    airport = parse_single_airport(stx_path)
    runways_by_dsg: dict[str, Runway] = {rw.designator: rw for rw in airport.runways}
    print(f"[STEP 2] Found {len(airport.runways)} runway(s): "
          f"{[rw.designator for rw in airport.runways]}")

    # ---- Step 3 — Match, validate, compute offsets -------------------------
    print(f"\n[STEP 3] Matching intersections → runways …")
    total = {"added": 0, "updated": 0, "skipped": 0, "errors": []}

    by_runway: dict[str, list[dict]] = {}
    for e in extracted_data:
        by_runway.setdefault(e["runway"], []).append(e)

    for dsg, entries in by_runway.items():
        runway = runways_by_dsg.get(dsg)
        if runway is None:
            msg = f"Runway '{dsg}' not found in .stx file"
            total["skipped"] += len(entries)
            total["errors"].append(msg)
            print(f"[WARN] {msg} — skipping {len(entries)} intersection(s)")
            continue
        st = _upsert_intersections(runway, entries, runway.tora)
        for k in ("added", "updated", "skipped"):
            total[k] += st[k]
        total["errors"].extend(st["errors"])

    # ---- Step 4 — Write updated .stx ---------------------------------------
    print(f"\n[STEP 4] Writing updated .stx → {output_path}")
    format_airport_to_file(airport, output_path)

    # ---- Summary -----------------------------------------------------------
    print(f"\n{'=' * 60}")
    print(f"Update complete  —  {total['added']} added  "
          f"{total['updated']} updated  {total['skipped']} skipped")
    if total["errors"]:
        print("Warnings / errors:")
        for err in total["errors"]:
            print(f"  • {err}")
    print(f"Output: {output_path}")

    return output_path


# ===================================================================
#  CLI
# ===================================================================

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Update STAS .stx with locally-extracted intersection data."
    )
    ap.add_argument("stx_path", help="Path to existing .stx file")
    ap.add_argument("chart_path", nargs="?", default=None,
                    help="Path to airport chart (PDF / image)")
    ap.add_argument("-o", "--output", help="Output path (default: overwrite stx)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract & validate only — do not write output")
    ap.add_argument("--from-json", help="Load pre-extracted intersection data from JSON file")
    args = ap.parse_args()

    # --- Dry-run mode -------------------------------------------------------
    if args.dry_run:
        print("=== DRY RUN ===\n")
        if args.from_json:
            with open(args.from_json, "r", encoding="utf-8") as fh:
                extracted = json.load(fh)
        elif args.chart_path:
            extracted = extract_intersections_from_chart(args.chart_path)
        else:
            ap.error("Dry-run requires chart_path or --from-json")

        if not extracted:
            print("No intersections extracted.")
            sys.exit(0)

        print(f"Extracted {len(extracted)} intersection(s):")
        for e in extracted:
            print(f"  RWY {e['runway']:5s}  {e['intersection_name']:8s}  TORA={e['tora']:.0f} m")

        # Validate against .stx
        airport = parse_single_airport(args.stx_path)
        rwy_map = {rw.designator: rw for rw in airport.runways}
        errors = 0
        for e in extracted:
            rw = rwy_map.get(e["runway"])
            if not rw:
                print(f"  → [ERROR] Runway '{e['runway']}' not found in .stx")
                errors += 1
                continue
            try:
                offset = compute_offset(rw.tora, e["tora"], e["runway"], e["intersection_name"])
                print(f"  → RWY {e['runway']}: offset = {rw.tora:.0f} − {e['tora']:.0f} = {offset:.0f} m")
            except TORAValidationError as exc:
                print(f"  → [ERROR] {exc}")
                errors += 1

        if errors:
            print(f"\n{errors} validation error(s) — review before running without --dry-run")
        else:
            print(f"\nAll valid — ready to run without --dry-run")
        sys.exit(0)

    # --- Real run -----------------------------------------------------------
    if args.from_json:
        with open(args.from_json, "r", encoding="utf-8") as fh:
            extracted_data = json.load(fh)
        update_stx_file(
            args.stx_path,
            extracted_data=extracted_data,
            output_path=args.output,
        )
    elif args.chart_path:
        update_stx_file(
            args.stx_path,
            args.chart_path,
            output_path=args.output,
        )
    else:
        ap.error("Either chart_path or --from-json is required.")
