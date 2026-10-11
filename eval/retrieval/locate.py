"""Helper for writing the retrieval gold set: find candidate evidence blocks in parsed documents.

Not a retriever under test - plain keyword matching over data/parsed/*.json, used by a person
(or Claude, once, while building the gold set) to pin evidence to doc / page / block.

  python eval/retrieval/locate.py --program ABP654 --agency fda --doc-type review \
      --all interchangeab --any switching "switch study" --top 15
  python eval/retrieval/locate.py --guidance --agency fda --all "comparative efficacy"
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def docs(db: Path, program: str | None, agency: str | None, doc_type: str | None,
         guidance: bool, doc_ids: list[int] | None):
    c = sqlite3.connect(db)
    q = ("select d.id, d.agency, d.doc_type, d.program_id, d.title, p.output_path "
         "from documents d join prep_documents p on p.document_id = d.id where p.status = 'ok'")
    args: list = []
    if program:
        q += " and d.program_id = ?"
        args.append(program)
    if agency:
        q += " and d.agency = ?"
        args.append(agency)
    if doc_type:
        q += " and d.doc_type = ?"
        args.append(doc_type)
    if guidance:
        q += " and d.doc_kind = 'guidance'"
    if doc_ids:
        q += f" and d.id in ({','.join('?' * len(doc_ids))})"
        args.extend(doc_ids)
    return c.execute(q, args).fetchall()


def block_text(b: dict) -> str:
    if b["type"] == "table":
        rows: dict = {}
        for cell in b["cells"]:
            rows.setdefault(cell["r"], []).append(cell["text"])
        return (b.get("caption") or "") + " || " + " | ".join(" ; ".join(v) for _, v in sorted(rows.items()))
    return b.get("text", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "ra.db"))
    ap.add_argument("--program")
    ap.add_argument("--agency")
    ap.add_argument("--doc-type")
    ap.add_argument("--doc-id", type=int, action="append")
    ap.add_argument("--guidance", action="store_true")
    ap.add_argument("--all", nargs="*", default=[], help="every term must occur (case-insensitive regex)")
    ap.add_argument("--any", nargs="*", default=[], help="at least one must occur")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--width", type=int, default=260)
    a = ap.parse_args()

    must = [re.compile(t, re.I) for t in a.all]
    some = [re.compile(t, re.I) for t in a.any]
    hits = []
    for did, agency, dtype, prog, title, path in docs(Path(a.db), a.program, a.agency, a.doc_type,
                                                      a.guidance, a.doc_id):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        for i, b in enumerate(d["blocks"]):
            if b.get("furniture"):
                continue
            t = block_text(b)
            if not all(p.search(t) for p in must):
                continue
            if some and not any(p.search(t) for p in some):
                continue
            score = sum(len(p.findall(t)) for p in must + some)
            hits.append((score, did, agency, dtype, prog, title, i, b, t))
    hits.sort(key=lambda h: -h[0])
    for score, did, agency, dtype, prog, title, i, b, t in hits[:a.top]:
        sec = " > ".join(b.get("section_path", [])[-2:])
        print(f"[doc {did} {agency}/{dtype} {prog}] p{b['page']} block#{i} {b['type']} "
              f"speaker={b.get('speaker')} status={b.get('status')}\n   title: {title[:80]}\n"
              f"   section: {sec[:140]}\n   {t[:a.width]}\n")
    print(f"{len(hits)} matching blocks", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
