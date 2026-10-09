"""Second OCR reader for OCR'd pages, used to catch and fix lines Docling's OCR garbled.

Docling's OCR occasionally merges or misreads a single line on a flattened page
("Tfhosyiir ss t y ts ...", "ne rei e est pid of its"). Which line breaks depends on the
exact crop, so an independent reading of the same page rarely breaks the same line.
We read each OCR page once more with RapidOCR (216 dpi, full page), map those lines onto
Docling's blocks and table cells by position, and per block:
  - the two readings agree                      -> keep Docling's text
  - they differ and the second reads as cleaner -> take the second reading ("reread")
  - they differ on numbers, or both look broken -> keep, flag "needs_review"
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import numpy as np
import pypdfium2 as pdfium

from .textquality import bad_token_count, garbled_spans, lm_score, ocr_gaps

_ENGINE = None
SCALE = 3.0  # 216 dpi


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR

        _ENGINE = RapidOCR(params={"Global.log_level": "error"})
    return _ENGINE


def page_lines(page: pdfium.PdfPage, scale: float = SCALE) -> list[tuple[list[float], str, float]]:
    """OCR lines of a whole page: (bbox [l, b, r, t] in PDF points bottom-left, text, score)."""
    w, h = page.get_size()
    img = page.render(scale=scale).to_numpy()
    res = _engine()(np.ascontiguousarray(img))
    if res is None or res.boxes is None:
        return []
    out = []
    for box, txt, sc in zip(res.boxes.tolist(), res.txts, res.scores):
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        out.append(([min(xs), h - max(ys), max(xs), h - min(ys)], txt, float(sc)))
    return out


def text_in(lines, bbox, tol: float = 2.0) -> str:
    """Lines whose centre falls in bbox, joined in reading order."""
    l, b, r, t = bbox
    hit = []
    for lb, txt, _ in lines:
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        if l - tol <= cx <= r + tol and b - tol <= cy <= t + tol:
            hit.append((lb, txt))
    hit.sort(key=lambda it: (-round((it[0][1] + it[0][3]) / 2 / 4), it[0][0]))
    return " ".join(txt for _, txt in hit)


def read_region(page: pdfium.PdfPage, bbox, scale: float, pad: float = 3.0) -> str:
    """Re-read one block on its own (cropped), at a given scale."""
    w, h = page.get_size()
    l, b, r, t = bbox
    crop = (max(0.0, l - pad), max(0.0, b - pad), max(0.0, w - r - pad), max(0.0, h - t - pad))
    img = page.render(scale=scale, crop=crop).to_numpy()
    if img.size == 0:
        return ""
    res = _engine()(np.ascontiguousarray(img))
    if res is None or res.boxes is None:
        return ""
    lines = []
    for box, txt in zip(res.boxes.tolist(), res.txts):
        ys = [p[1] for p in box]
        lines.append(([min(p[0] for p in box), -max(ys), max(p[0] for p in box), -min(ys)], txt, 1.0))
    return text_in(lines, (-1e9, -1e9, 1e9, 1e9))


_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _norm(s: str) -> str:
    return _WS.sub("", s)


def _rank(text: str) -> tuple:
    """Lower is better: broken fragment runs first, then count of odd tokens, then
    letter-trigram plausibility."""
    return (garbled_spans(text) or ocr_gaps(text), bad_token_count(text), -lm_score(text))


def decide(first: str, second: str, rereads=None) -> tuple[str, str]:
    """Pick the reading of one OCR block. Returns (text, status), status in
    {"kept", "reread", "needs_review"}.

    first   Docling's OCR text; second  the page-level second reader's text for the block
    rereads callable(scale) -> text, a cropped re-read, used only when the block is suspect
    """
    a, b = _norm(first), _norm(second)
    if a == b:
        return first, "kept"
    similar = bool(b) and SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.95
    comparable = bool(b) and 0.8 * len(a) <= len(b) <= 1.25 * len(a)
    numbers_differ = comparable and _NUM.findall(first) != _NUM.findall(second)
    broken = garbled_spans(first) or ocr_gaps(first)
    suspect = broken or (bool(b) and not similar) or numbers_differ
    if not suspect:
        return first, "kept"
    readings = [first]
    # a much shorter second reading means its detector missed lines: not a usable reading
    if b and len(b) >= 0.8 * len(a):
        readings.append(second)
    if rereads is not None and broken:
        for scale in (4.0, 2.0, 5.0, 2.5):
            t = rereads(scale)
            if t and len(_norm(t)) >= 0.8 * len(a):
                readings.append(t)
    best = min(readings, key=_rank)
    status = "kept" if best is first else "reread"
    if garbled_spans(best) or ocr_gaps(best):
        status = "needs_review"
    # numbers must agree across the comparable readings, otherwise a person checks
    nums = {tuple(_NUM.findall(r)) for r in readings}
    if len(nums) > 1 and status != "reread":
        status = "needs_review"
    return best, status
