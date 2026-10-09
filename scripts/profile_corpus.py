"""Per-page profile of every stored PDF (dev tool, uses PyMuPDF/AGPL): the signals a preprocessing router would use.
Usage: python scripts/profile_corpus.py data/ra.db profile.json"""
import collections
import json
import re
import sqlite3
import sys
import time

import pymupdf

DB = sys.argv[1]
OUT = sys.argv[2]

REDACT = re.compile(r"\(b\)\s*\(4\)|\(b\)\(6\)|\[REDACTED\]|confidential", re.I)
LINE_NO = re.compile(r"^\s*\d{1,4}\s*$")


def page_features(page, do_tables):
    W, H = page.rect.width, page.rect.height
    area = W * H
    text = page.get_text("text")
    chars = len(text.strip())
    # images and how much of the page they cover
    img_area, n_img = 0.0, 0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        a = max(0, x1 - x0) * max(0, y1 - y0)
        if a > 0:
            n_img += 1
            img_area += a
    img_cov = min(1.0, img_area / area) if area else 0
    # vector drawings (table rules, charts)
    n_draw = len(page.get_drawings())
    blocks = page.get_text("blocks")
    text_blocks = [b for b in blocks if b[6] == 0 and b[4].strip()]
    # two-column guess: text blocks whose x0 sit in two clearly separated clusters
    lefts = sorted(round(b[0] / W, 2) for b in text_blocks if (b[2] - b[0]) < 0.55 * W)
    two_col = bool(lefts) and sum(1 for x in lefts if x > 0.45) >= 3 and sum(1 for x in lefts if x < 0.2) >= 3
    # left-margin line numbers (FDA draft guidances)
    words = page.get_text("words")
    margin_nums = sum(1 for w in words if w[0] < 0.1 * W and w[4].isdigit() and len(w[4]) <= 4)
    # top/bottom lines for header/footer detection
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    head = lines[0][:80] if lines else ""
    foot = lines[-1][:80] if lines else ""
    redactions = len(REDACT.findall(text))
    # black rectangles = redaction boxes
    black_boxes = 0
    for d in page.get_drawings():
        if d.get("fill") and all(c < 0.1 for c in d["fill"]) and d["rect"].width * d["rect"].height > 200:
            black_boxes += 1
    nonascii = sum(1 for ch in text if ord(ch) > 127 and not ch.isalpha()) / max(1, len(text))
    n_tables = 0
    if do_tables:
        try:
            n_tables = len(page.find_tables().tables)
        except Exception:
            n_tables = -1
    return dict(chars=chars, img_cov=round(img_cov, 2), n_img=n_img, n_draw=n_draw,
                two_col=two_col, margin_nums=margin_nums, head=head, foot=foot,
                redactions=redactions, black_boxes=black_boxes, nonascii=round(nonascii, 3),
                n_tables=n_tables, rot=page.rotation, w=round(W), h=round(H))


def main():
    c = sqlite3.connect(DB)
    rows = c.execute("""select d.id, d.agency, d.doc_kind, d.doc_type, coalesce(d.native_doc_type,''),
                        d.program_id, d.title, v.storage_path from documents d
                        join document_versions v on v.document_id=d.id
                        where d.fetch_status='fetched' and v.storage_path like '%.pdf'""").fetchall()
    out = []
    t0 = time.time()
    for did, ag, kind, dt, nd, pid, title, path in rows:
        t = time.time()
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            out.append(dict(doc_id=did, error=str(e)))
            continue
        meta = doc.metadata or {}
        pages = []
        for i, page in enumerate(doc):
            pages.append(page_features(page, do_tables=True))
        out.append(dict(doc_id=did, agency=ag, kind=kind, doc_type=dt, native=nd, program=pid,
                        title=title[:90], pages=len(doc), toc=len(doc.get_toc()),
                        producer=meta.get("producer", ""), creator=meta.get("creator", ""),
                        tagged=bool(doc.pdf_catalog() and "StructTreeRoot" in doc.xref_object(doc.pdf_catalog())),
                        secs=round(time.time() - t, 2), page_features=pages))
        doc.close()
    json.dump(out, open(OUT, "w"))
    print(f"{len(out)} docs, {sum(d.get('pages', 0) for d in out)} pages in {time.time() - t0:.0f}s")


main()
