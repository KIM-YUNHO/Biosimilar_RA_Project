"""Page router: decides per page which extraction setting to use. Pure rules over PageSignals.

Routes and what they mean for extraction:
  DIGITAL      text layer is good; no OCR. Tables by TableFormer.
  MIXED        good text layer plus bitmaps without text (figures, pasted table screenshots);
               OCR only the bitmap regions.
  IMAGE        no text layer, page is a picture (FDA flattened pages). OCR + TableFormer.
  ARTWORK      IMAGE inside a label document (carton / container artwork). Low search weight.
  LEGACY_OCR   scan with an invisible OCR text layer already in the file. Use that layer;
               re-running OCR on these added noise in the 2026-10-09 test.
  BROKEN_TEXT  a text layer exists but decodes to garbage (fonts without a usable
               ToUnicode map, e.g. "LQWHUFKDQJHDEOH"). Full-page OCR.
  BLANK        nothing to extract.
"""
from __future__ import annotations

from .signals import PageSignals

ROUTES = ("DIGITAL", "MIXED", "IMAGE", "ARTWORK", "LEGACY_OCR", "BROKEN_TEXT", "BLANK")

# Docling OCR mode for each route: None = OCR off, "auto" = bitmap regions, "full" = whole page
OCR_MODE = {
    "DIGITAL": None, "LEGACY_OCR": None,
    "MIXED": "auto", "IMAGE": "auto", "ARTWORK": "auto",
    "BROKEN_TEXT": "full",
    "BLANK": None,
}


def route_page(s: PageSignals, doc_type: str | None = None) -> tuple[str, str]:
    """Return (route, reason)."""
    if s.chars < 30 and s.img_cov < 0.05:
        return "BLANK", f"chars={s.chars} img_cov={s.img_cov}"
    if s.chars >= 200 and s.garble >= 0.4:
        return "BROKEN_TEXT", f"garble={s.garble}"
    if s.chars < 50 and s.img_cov >= 0.3:
        route = "ARTWORK" if doc_type == "label" else "IMAGE"
        return route, f"chars={s.chars} img_cov={s.img_cov}"
    if s.img_cov >= 0.7 and s.invisible_text >= 0.5:
        return "LEGACY_OCR", f"img_cov={s.img_cov} invisible_text={s.invisible_text}"
    if s.bare_img_cov >= 0.05:
        return "MIXED", f"bare_img_cov={s.bare_img_cov}"
    return "DIGITAL", f"chars={s.chars}"


def route_document(signals: list[PageSignals], doc_type: str | None = None) -> list[dict]:
    out = []
    for s in signals:
        route, reason = route_page(s, doc_type)
        out.append({"page": s.page, "route": route, "reason": reason, "signals": s.to_dict()})
    return out
