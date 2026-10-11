"""Check the retrieval gold set against the parsed documents: every evidence quote must occur
(after normalisation) in a block of the named document on the named page.

  python eval/retrieval/check_gold.py            # report, exit 1 if any quote is not found
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def norm(text: str) -> str:
    """The same normalisation the retrieval scorer uses: NFKC, lower case, no whitespace."""
    return "".join(unicodedata.normalize("NFKC", text).lower().split())


def block_text(b: dict) -> str:
    if b["type"] == "table":
        return (b.get("caption") or "") + " " + " ".join(c["text"] for c in b["cells"])
    return b.get("text") or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "ra.db"))
    ap.add_argument("--gold", default=str(ROOT / "eval" / "retrieval" / "questions.yaml"))
    a = ap.parse_args()
    c = sqlite3.connect(a.db)
    gold = yaml.safe_load(Path(a.gold).read_text(encoding="utf-8"))
    cache: dict[int, list[dict]] = {}

    def pages_of(doc: int) -> dict[int, str]:
        if doc not in cache:
            row = c.execute("select output_path from prep_documents where document_id = ? and status = 'ok'",
                            (doc,)).fetchone()
            cache[doc] = json.loads(Path(row[0]).read_text(encoding="utf-8"))["blocks"] if row else []
        out: dict[int, str] = {}
        for b in cache[doc]:
            out[b["page"]] = out.get(b["page"], "") + " " + norm(block_text(b))
        return out

    bad = groups = pending = 0
    for q in gold["questions"]:
        for g in q.get("evidence", []):
            groups += 1
            if "pending" in g:
                pending += 1
                continue
            for ev in g["any"]:
                text = pages_of(ev["doc"]).get(ev["page"], "")
                if norm(ev["quote"]) not in text:
                    bad += 1
                    print(f"{q['id']} {g['group']}: not found doc {ev['doc']} p{ev['page']}: {ev['quote']}")
    print(f"{len(gold['questions'])} questions, {groups} evidence groups ({pending} pending), "
          f"{bad} quotes not found")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
