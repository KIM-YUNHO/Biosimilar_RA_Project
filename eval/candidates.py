"""Run each extraction candidate on the gold pages and save a common output format.

Common format per page (eval/out/<candidate>/<gid>.json):
  {"text": [str, ...],                 # text blocks outside tables, reading order
   "tables": [[[cell, ...], ...], ...], # each table as rows of origin cells (spans once)
   "seconds": float, "flags": {...}}

Candidates:
  A0  Docling with per-page route settings and watermark removal (ra_prep), nothing else
  A1  A0 + second OCR reader: garbled-line repair and picture-to-table rebuild (proposed)
  A2  A1 but scans with an embedded OCR layer are re-OCR'd (full page)
  B   PaddleOCR-VL 1.6 (OCR pages only; markdown produced by run_paddle_gold.py)
  C   baseline: PyMuPDF text + find_tables; OCR pages read by RapidOCR as plain lines

Usage: python eval/candidates.py A0 A1 C [--pages g01,g02]
       python eval/candidates.py B --paddle-dir /path/to/paddle/markdown
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from ra_prep.route import route_page  # noqa: E402
from ra_prep.signals import page_signals  # noqa: E402

GOLD = ROOT / "gold"
OUT = ROOT / "out"


def manifest() -> list[dict]:
    return yaml.safe_load((GOLD / "manifest.yaml").read_text())["pages"]


def page_pdf(gid: str) -> str:
    return str(GOLD / "pages" / f"{gid}.pdf")


def routes_for(gid: str, doc_type: str) -> list[dict]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(page_pdf(gid))
    s = page_signals(pdf, 0)
    pdf.close()
    route, reason = route_page(s, doc_type)
    return [{"page": 1, "route": route, "reason": reason, "signals": s.to_dict()}]


# -- A: ra_prep / Docling ---------------------------------------------------------------
def tables_from_blocks(blocks: list[dict]) -> list[list[list[str]]]:
    out = []
    for b in blocks:
        if b["type"] != "table":
            continue
        rows: dict[int, list] = {}
        for c in b["cells"]:
            rows.setdefault(c["r"], []).append((c["c"], c["text"]))
        out.append([[t for _, t in sorted(rows[r])] for r in sorted(rows)])
    return out


def run_a(pages, repair: bool, name: str, legacy_ocr: bool = False):
    from ra_prep import route as route_mod
    from ra_prep.extract import Extractor
    from ra_prep.pipeline import process_pdf

    if legacy_ocr:  # variant: re-OCR scans instead of trusting their embedded OCR layer
        route_mod.OCR_MODE["LEGACY_OCR"] = "full"
    ex = Extractor(repair_ocr=repair, picture_tables=repair)
    # warm up model loading so timings are per page
    process_pdf(page_pdf(pages[0]["id"]), pages[0]["doc_type"], ex)
    for e in pages:
        t = time.time()
        res = process_pdf(page_pdf(e["id"]), e["doc_type"], ex)
        el = time.time() - t
        blocks = res["blocks"]
        route = res["routes"][0]["route"]
        text = [b["text"] for b in blocks if b["type"] == "text" and b["text"].strip()]
        flags = {"route": route, "watermarks_removed": res["watermarks_removed"],
                 "needs_review": sum(1 for b in blocks if b.get("status") == "needs_review"),
                 "reread": sum(1 for b in blocks if b.get("status") == "reread")
                 + sum(1 for b in blocks if b["type"] == "table"
                       for c in b["cells"] if c.get("status") == "reread")}
        save(name, e["id"], {"text": text, "tables": tables_from_blocks(blocks),
                             "seconds": round(el, 2), "flags": flags, "blocks": blocks})
        print(name, e["id"], route, f"{el:.1f}s", flags, flush=True)


# -- B: PaddleOCR-VL markdown -----------------------------------------------------------
class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self._rows, self._row, self._cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data.replace("\\n", " "))


_TABLE_RE = re.compile(r"<table.*?</table>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_markdown(md: str) -> tuple[list[str], list]:
    p = _TableParser()
    for t in _TABLE_RE.findall(md):
        p.feed(t)
    rest = _TABLE_RE.sub("\n\n", md)
    text = []
    for para in re.split(r"\n\s*\n", rest):
        para = html.unescape(_TAG_RE.sub("", para)).strip().lstrip("#").strip()
        para = re.sub(r"\$\s*\\mu\s*", "μ", para).replace("$", "")
        if para:
            text.append(" ".join(para.split()))
    return text, p.tables


def run_b(pages, paddle_dir: Path):
    times = json.loads((paddle_dir / "times.json").read_text()) if (paddle_dir / "times.json").exists() else {}
    for e in pages:
        f = paddle_dir / f"{e['id']}.md"
        if not f.exists():
            continue
        text, tables = parse_markdown(f.read_text())
        save("B", e["id"], {"text": text, "tables": tables, "seconds": times.get(e["id"]),
                            "flags": {}})
        print("B", e["id"], len(text), len(tables))


# -- C: baseline ------------------------------------------------------------------------
def run_c(pages):
    import pymupdf
    import pypdfium2 as pdfium

    from ra_prep.reocr import page_lines

    for e in pages:
        r = routes_for(e["id"], e["doc_type"])[0]
        t = time.time()
        doc = pymupdf.open(page_pdf(e["id"]))
        page = doc[0]
        tables, text = [], []
        if r["route"] in ("IMAGE", "ARTWORK", "BROKEN_TEXT"):
            pdf = pdfium.PdfDocument(page_pdf(e["id"]))
            lines = page_lines(pdf[0])
            pdf.close()
            lines.sort(key=lambda it: (-round((it[0][1] + it[0][3]) / 2 / 4), it[0][0]))
            text = [t_ for _, t_, _ in lines]
        else:
            boxes = []
            for tb in page.find_tables().tables:
                rows = [[" ".join((c or "").split()) for c in row if c is not None] for row in tb.extract()]
                tables.append(rows)
                boxes.append(pymupdf.Rect(tb.bbox))
            for b in page.get_text("blocks"):
                rect = pymupdf.Rect(b[:4])
                if b[6] == 0 and b[4].strip() and not any(bx.contains(rect) for bx in boxes):
                    text.append(" ".join(b[4].split()))
        el = time.time() - t
        save("C", e["id"], {"text": text, "tables": tables, "seconds": round(el, 2),
                            "flags": {"route": r["route"]}})
        print("C", e["id"], r["route"], f"{el:.1f}s", flush=True)


def save(cand: str, gid: str, obj: dict):
    d = OUT / cand
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{gid}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", nargs="+")
    ap.add_argument("--pages")
    ap.add_argument("--paddle-dir", default="/tmp/claude-0/bench/paddle_gold")
    a = ap.parse_args()
    pages = manifest()
    if a.pages:
        keep = set(a.pages.split(","))
        pages = [p for p in pages if p["id"] in keep]
    for c in a.candidates:
        if c == "A0":
            run_a(pages, repair=False, name="A0")
        elif c == "A1":
            run_a(pages, repair=True, name="A1")
        elif c == "A2":
            run_a(pages, repair=True, name="A2", legacy_ocr=True)
        elif c == "B":
            run_b(pages, Path(a.paddle_dir))
        elif c == "C":
            run_c(pages)


if __name__ == "__main__":
    main()
