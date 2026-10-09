"""Rebuild a table from OCR line boxes when the layout model called it a picture.

Pasted table screenshots in EMA reports are sometimes classified as pictures, so no table
structure model runs on them and their numbers come out as loose words (gold page g05).
For picture regions whose OCR text is mostly numbers laid out in rows we rebuild a grid:
rows by vertical position, columns from the x-extents of the numeric rows. Cells get
structure "ocr_grid" so later stages can treat them as lower confidence.
"""
from __future__ import annotations

import re
from statistics import median

_NUMERIC = re.compile(r"^[\s(<>≤≥±~\-–]*\d[\d.,:/%()\s\-–±]*$")


def _is_num(t: str) -> bool:
    return bool(_NUMERIC.match(t.strip()))


def lines_in(lines, bbox, tol: float = 2.0):
    l, b, r, t = bbox
    out = []
    for lb, txt, _ in lines:
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        if l - tol <= cx <= r + tol and b - tol <= cy <= t + tol and txt.strip():
            out.append((lb, txt.strip()))
    return out


def looks_tabular(items) -> bool:
    if len(items) < 8:
        return False
    nums = sum(1 for _, t in items if _is_num(t))
    return nums / len(items) >= 0.35 and len(_rows(items)) >= 3


def _rows(items):
    h = median(lb[3] - lb[1] for lb, _ in items) or 1.0
    rows: list[list] = []
    for lb, t in sorted(items, key=lambda it: -(it[0][1] + it[0][3]) / 2):
        cy = (lb[1] + lb[3]) / 2
        if rows and abs(rows[-1][0] - cy) <= 0.5 * h:
            rows[-1][1].append((lb, t))
        else:
            rows.append([cy, [(lb, t)]])
    return [sorted(r[1], key=lambda it: it[0][0]) for r in rows]


def _columns(rows):
    """Column x-intervals from rows that are mostly numeric (data rows), merged where they
    overlap; label column is everything left of the first numeric column."""
    spans = []
    for row in rows:
        nums = [lb for lb, t in row if _is_num(t)]
        if len(nums) >= max(2, len(row) // 2):
            spans.extend((lb[0], lb[2]) for lb in nums)
    if not spans:
        return []
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + 2:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def build(items) -> list[dict]:
    """Return cells in the extractor's table-cell format."""
    rows = _rows(items)
    cols = _columns(rows)
    cells = []
    for r, row in enumerate(rows):
        slots: dict[int, list[str]] = {}
        for lb, t in row:
            cx = (lb[0] + lb[2]) / 2
            if not cols or lb[2] < cols[0][0]:
                c = 0  # label column
            else:
                c = 1 + min(range(len(cols)), key=lambda k: 0 if cols[k][0] <= cx <= cols[k][1]
                            else min(abs(cx - cols[k][0]), abs(cx - cols[k][1])))
            slots.setdefault(c, []).append(t)
        for c in sorted(slots):
            cells.append({"r": r, "c": c, "rs": 1, "cs": 1, "text": " ".join(slots[c]),
                          "header": False, "bbox": None, "structure": "ocr_grid"})
    return cells
