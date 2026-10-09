"""Extraction: Docling with per-route settings, then a second OCR reader on OCR'd pages
(garbled-line repair, table rebuild for table images the layout model calls pictures).

Output is a list of plain-dict blocks (JSON-serialisable), in reading order:
  {"type": "text", "label": "text|section_header|list_item|caption|footnote|page_header|...",
   "text": str, "page": int, "bbox": [l, b, r, t], "source": "text_layer|ocr",
   "layer": "body|furniture", "status": "kept|reread|needs_review"}
  {"type": "table", ..., "caption": str, "n_rows": int, "n_cols": int,
   "cells": [{"r", "c", "rs", "cs", "text", "header", "bbox"}]}
  {"type": "picture", ..., "caption": str}
  table cells may carry "status" (reread | needs_review); rebuilt tables carry
  "structure": "ocr_grid".
bbox is in PDF points with a bottom-left origin, the same frame pypdfium2 uses.
"""
from __future__ import annotations

import logging
import time
from itertools import groupby

import pypdfium2 as pdfium

from . import ENGINE_VERSION
from . import grid
from .reocr import decide, page_lines, read_region, text_in
from .route import OCR_MODE

_log = logging.getLogger(__name__)


class Extractor:
    def __init__(self, num_threads: int = 4, table_mode: str = "accurate",
                 artifacts_path: str | None = None, repair_ocr: bool = True,
                 picture_tables: bool = True, device: str = "auto"):
        self.num_threads = num_threads
        self.table_mode = table_mode
        self.artifacts_path = artifacts_path
        self.repair_ocr = repair_ocr
        self.picture_tables = picture_tables
        self.device = device
        self._converters: dict = {}

    def version(self) -> str:
        import docling

        dv = getattr(docling, "__version__", None)
        if dv is None:
            from importlib.metadata import version as _v
            dv = _v("docling")
        return (f"{ENGINE_VERSION}+docling{dv}+tables-{self.table_mode}"
                f"+repair{int(self.repair_ocr)}+pictables{int(self.picture_tables)}")

    # -- docling setup ---------------------------------------------------------------
    def _converter(self, ocr_mode: str | None):
        if ocr_mode in self._converters:
            return self._converters[ocr_mode]
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions, RapidOcrOptions, TableFormerMode)
        from docling.document_converter import DocumentConverter, PdfFormatOption

        o = PdfPipelineOptions()
        if self.artifacts_path:
            o.artifacts_path = self.artifacts_path
        o.accelerator_options = AcceleratorOptions(num_threads=self.num_threads, device=self.device)
        o.do_ocr = ocr_mode is not None
        if o.do_ocr:
            o.ocr_options = RapidOcrOptions(force_full_page_ocr=(ocr_mode == "full"))
        o.do_table_structure = True
        o.table_structure_options.mode = (
            TableFormerMode.ACCURATE if self.table_mode == "accurate" else TableFormerMode.FAST)
        o.table_structure_options.do_cell_matching = True
        conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})
        self._converters[ocr_mode] = conv
        return conv

    # -- main entry ------------------------------------------------------------------
    def extract(self, path: str, routes: list[dict]) -> dict:
        """routes: output of route.route_document (one dict per page, in page order)."""
        by_page = {r["page"]: r for r in routes}
        blocks: list[dict] = []
        timing: dict[str, float] = {}
        todo = [r for r in routes if r["route"] != "BLANK"]
        # consecutive pages sharing an OCR mode are converted in one call
        runs = []
        for mode, grp in groupby(todo, key=lambda r: OCR_MODE[r["route"]]):
            pages = [r["page"] for r in grp]
            # split where pages are not contiguous (a BLANK page in between)
            start = prev = pages[0]
            for p in pages[1:]:
                if p != prev + 1:
                    runs.append((mode, start, prev))
                    start = p
                prev = p
            runs.append((mode, start, prev))

        for mode, first, last in runs:
            t = time.time()
            res = self._converter(mode).convert(path, page_range=(first, last), raises_on_error=False)
            if res.document is None:
                _log.warning("docling failed on %s pages %s-%s", path, first, last)
                continue
            blocks.extend(self._blocks(res.document, by_page))
            timing[str(mode)] = timing.get(str(mode), 0.0) + time.time() - t

        if self.repair_ocr or self.picture_tables:
            t = time.time()
            lines = self._second_reader(path, blocks, by_page)
            if self.repair_ocr:
                self._repair(path, blocks, lines)
            if self.picture_tables:
                blocks = self._pictures_to_tables(blocks, lines)
            timing["second_reader"] = time.time() - t
        return {"engine": self.version(), "blocks": blocks, "timing": timing}

    # -- HTML (HC SBD / RDS pages) ------------------------------------------------------
    def extract_html(self, path: str) -> dict:
        """HTML pages are already structured: DOM walk of <main> (see html.py)."""
        from .html import html_blocks

        t = time.time()
        raw = open(path, "rb").read().decode("utf-8", errors="replace")
        return {"engine": self.version(), "blocks": html_blocks(raw),
                "timing": {"html": time.time() - t}}

    # -- docling document -> blocks ---------------------------------------------------
    def _blocks(self, doc, by_page: dict) -> list[dict]:
        from docling_core.types.doc import ContentLayer, PictureItem, TableItem, TextItem
        from docling_core.types.doc.base import CoordOrigin

        def bbox_of(prov, page_h):
            bb = prov.bbox
            if bb.coord_origin == CoordOrigin.TOPLEFT:
                bb = bb.to_bottom_left_origin(page_h)
            return [round(bb.l, 1), round(bb.b, 1), round(bb.r, 1), round(bb.t, 1)]

        def source(page_no, bbox):
            r = by_page.get(page_no, {})
            route = r.get("route")
            if route in ("IMAGE", "ARTWORK", "BROKEN_TEXT"):
                return "ocr"
            if route == "MIXED":
                for b in r.get("signals", {}).get("bare_boxes", []):
                    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                    if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                        return "ocr"
            return "text_layer"

        out = []
        layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
        for item, _level in doc.iterate_items(included_content_layers=layers, traverse_pictures=True):
            if not getattr(item, "prov", None):
                continue
            prov = item.prov[0]
            page_no = prov.page_no
            page_h = doc.pages[page_no].size.height
            bbox = bbox_of(prov, page_h)
            parent = item.parent.cref if getattr(item, "parent", None) is not None else None
            base = {"ref": item.self_ref, "parent": parent,
                    "page": page_no, "bbox": bbox, "layer": item.content_layer.value,
                    "route": by_page.get(page_no, {}).get("route"), "source": source(page_no, bbox)}
            if isinstance(item, TableItem):
                cells = []
                for c in item.data.table_cells:
                    cb = None
                    if c.bbox is not None:
                        b = c.bbox
                        if b.coord_origin == CoordOrigin.TOPLEFT:
                            b = b.to_bottom_left_origin(page_h)
                        cb = [round(b.l, 1), round(b.b, 1), round(b.r, 1), round(b.t, 1)]
                    cells.append({"r": c.start_row_offset_idx, "c": c.start_col_offset_idx,
                                  "rs": c.row_span, "cs": c.col_span, "text": c.text,
                                  "header": bool(c.column_header), "bbox": cb})
                out.append({"type": "table", **base, "caption": item.caption_text(doc),
                            "n_rows": item.data.num_rows, "n_cols": item.data.num_cols,
                            "cells": cells, "status": "kept"})
            elif isinstance(item, PictureItem):
                out.append({"type": "picture", **base, "caption": item.caption_text(doc),
                            "text": "", "status": "kept"})
            elif isinstance(item, TextItem):
                out.append({"type": "text", **base, "label": item.label.value, "text": item.text,
                            "status": "kept"})
        return out

    # -- second reader -----------------------------------------------------------------
    def _second_reader(self, path: str, blocks: list[dict], by_page: dict) -> dict[int, list]:
        """One independent RapidOCR pass over every page that has OCR-sourced blocks
        (whole page; on MIXED pages only the bitmap regions without text)."""
        ocr_pages = sorted({b["page"] for b in blocks if b["source"] == "ocr"})
        if not ocr_pages:
            return {}
        pdf = pdfium.PdfDocument(path)
        try:
            out = {}
            for p in ocr_pages:
                page = pdf[p - 1]
                r = by_page.get(p, {})
                regions = r.get("signals", {}).get("bare_boxes") if r.get("route") == "MIXED" else None
                out[p] = page_lines(page, regions=regions)
                page.close()
            return out
        finally:
            pdf.close()

    # -- OCR repair --------------------------------------------------------------------
    def _repair(self, path: str, blocks: list[dict], lines_by_page: dict) -> None:
        """Check every OCR-sourced text block and table cell against the second reader;
        suspect blocks get a third, cropped read at other scales (see reocr.decide)."""
        if not lines_by_page:
            return
        pdf = pdfium.PdfDocument(path)
        pages = {p: pdf[p - 1] for p in lines_by_page}
        try:
            for blk in blocks:
                if blk["source"] != "ocr":
                    continue
                page, lines = pages[blk["page"]], lines_by_page[blk["page"]]
                if blk["type"] == "text":
                    bbox = blk["bbox"]
                    blk["text"], blk["status"] = decide(
                        blk["text"], text_in(lines, bbox),
                        rereads=lambda sc, pg=page, bb=bbox: read_region(pg, bb, sc))
                elif blk["type"] == "table":
                    for c in blk["cells"]:
                        if not c["bbox"] or not c["text"].strip():
                            continue
                        bbox = c["bbox"]
                        c["text"], st = decide(
                            c["text"], text_in(lines, bbox, tol=0.5),
                            rereads=lambda sc, pg=page, bb=bbox: read_region(pg, bb, sc))
                        if st != "kept":
                            c["status"] = st
                        if st == "needs_review":
                            blk["status"] = "needs_review"
        finally:
            for p in pages.values():
                p.close()
            pdf.close()

    # -- pictures that are really tables -------------------------------------------------
    def _pictures_to_tables(self, blocks: list[dict], lines_by_page: dict) -> list[dict]:
        out, dropped = [], {}
        for blk in blocks:
            if blk["type"] == "picture" and blk["source"] == "ocr" and blk["page"] in lines_by_page:
                items = grid.lines_in(lines_by_page[blk["page"]], blk["bbox"])
                if grid.looks_tabular(items):
                    cells = grid.build(items)
                    blk = {**blk, "type": "table", "structure": "ocr_grid", "cells": cells,
                           "n_rows": 1 + max(c["r"] for c in cells),
                           "n_cols": 1 + max(c["c"] for c in cells)}
                    dropped[blk["ref"]] = blk["bbox"]
            out.append(blk)
        # the picture's own OCR words are now inside the table; its caption stays
        def inside(b, box):
            cx, cy = (b["bbox"][0] + b["bbox"][2]) / 2, (b["bbox"][1] + b["bbox"][3]) / 2
            return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]

        return [b for b in out
                if not (b["type"] == "text" and b.get("parent") in dropped
                        and inside(b, dropped[b["parent"]]))]
