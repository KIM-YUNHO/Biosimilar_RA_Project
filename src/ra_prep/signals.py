"""Per-page signals read straight from the PDF (pypdfium2, no models). Input to the router."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .textquality import garble_score


@dataclass
class PageSignals:
    page: int                 # 1-based
    width: float
    height: float
    chars: int                # characters in the text layer
    img_cov: float            # share of the page covered by bitmaps
    bare_img_cov: float       # share covered by bitmaps with no text-layer text on top
    invisible_text: float     # share of text objects drawn invisible (OCR layer over a scan)
    n_text_objs: int
    n_paths: int              # vector paths: table rules, charts
    garble: float             # 0 clean .. 1 garbage text layer (broken font encoding)
    bare_boxes: list          # bitmaps without text on top, (l, b, r, t) PDF points, bottom-left origin

    def to_dict(self) -> dict:
        return asdict(self)


def _area(b) -> float:
    l, bt, r, t = b
    return max(0.0, r - l) * max(0.0, t - bt)


def _inside(inner, outer, tol: float = 2.0) -> bool:
    cx = (inner[0] + inner[2]) / 2
    cy = (inner[1] + inner[3]) / 2
    return outer[0] - tol <= cx <= outer[2] + tol and outer[1] - tol <= cy <= outer[3] + tol


def page_signals(pdf: pdfium.PdfDocument, index: int) -> PageSignals:
    page = pdf[index]
    w, h = page.get_size()
    page_area = w * h or 1.0
    tp = page.get_textpage()
    text = tp.get_text_range()
    chars = len(text.strip())

    imgs, texts, n_paths, invisible = [], [], 0, 0
    for obj in page.get_objects(max_depth=4):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            b = obj.get_bounds()
            # clip to the page: some scans overhang the media box
            b = (max(0, b[0]), max(0, b[1]), min(w, b[2]), min(h, b[3]))
            if _area(b) > 0.002 * page_area:
                imgs.append(b)
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            b = obj.get_bounds()
            if b[2] - b[0] < 0.5 or b[3] - b[1] < 0.5:
                continue  # empty text objects (seen at EMA figure corners)
            texts.append(b)
            if pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw) == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE:
                invisible += 1
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            n_paths += 1

    img_area = min(page_area, sum(_area(b) for b in imgs))
    bare, bare_boxes = 0.0, []
    for b in imgs:
        # a bitmap counts as covered when real text sits on it (scan OCR layer, background
        # artwork behind digital text). Measured as the share of the bitmap's area under
        # text boxes, so a caption overlapping the edge of a pasted table image or a few
        # labels over a chart do not count.
        covered = sum(_area(t) for t in texts if _inside(t, b)) / (_area(b) or 1.0)
        if covered < 0.08:
            bare += _area(b)
            bare_boxes.append([round(v, 1) for v in b])
    tp.close()
    page.close()
    return PageSignals(
        page=index + 1, width=round(w, 1), height=round(h, 1), chars=chars,
        img_cov=round(img_area / page_area, 3), bare_img_cov=round(min(1.0, bare / page_area), 3),
        invisible_text=round(invisible / len(texts), 3) if texts else 0.0,
        n_text_objs=len(texts), n_paths=n_paths, garble=round(garble_score(text), 3),
        bare_boxes=bare_boxes,
    )


def document_signals(path: str) -> list[PageSignals]:
    pdf = pdfium.PdfDocument(path)
    try:
        return [page_signals(pdf, i) for i in range(len(pdf))]
    finally:
        pdf.close()
