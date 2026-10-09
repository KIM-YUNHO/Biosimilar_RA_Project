"""PDF clean-up before routing: remove diagonal watermark text.

EMA marks withdrawn products with a diagonal "Medicinal product no longer authorised"
text object drawn over the page. Left in place it (a) gets merged into paragraphs and table
cells and (b) sits on top of pasted table images, so the router thinks those images are
covered by text and skips OCR (gold pages g03, g06 lost whole tables this way).
Only top-level text objects rotated well off the horizontal are removed; regular text,
rotated page content (/Rotate) and vertical table headers (90 degrees) are left alone.
"""
from __future__ import annotations

import math
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c


def _diagonal(obj) -> bool:
    a, b, _c, _d, _e, _f = obj.get_matrix().get()
    ang = abs(math.degrees(math.atan2(b, a))) % 180
    ang = min(ang, 180 - ang)  # 0..90
    return 15 <= ang <= 75


def remove_watermarks(src: str, dst: str) -> int:
    """Write a copy of src without diagonal text objects to dst. Returns the number of
    objects removed (0 means dst was not written)."""
    pdf = pdfium.PdfDocument(src)
    removed = 0
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            hits = [o for o in page.get_objects(max_depth=1)
                    if o.type == pdfium_c.FPDF_PAGEOBJ_TEXT and o.level == 0 and _diagonal(o)]
            for o in hits:
                page.remove_obj(o)
                o.close()  # removed objects are owned by the caller
            if hits:
                page.gen_content()
                removed += len(hits)
            page.close()
        if removed:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            pdf.save(dst)
    finally:
        pdf.close()
    return removed
