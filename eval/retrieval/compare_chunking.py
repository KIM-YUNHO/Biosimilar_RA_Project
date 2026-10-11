"""Chunking comparison on the retrieval gold set (eval/retrieval/questions.yaml).

Variants (docs/chunking-strategy.md 6):
  V1  one parsed block = one chunk, whole tables, no parents
  V2  section chunks of ~400 tokens, whole tables, parents
  V3  V2 + table row chunks
  V4  section chunks of ~800 tokens, whole tables + row chunks, parents

Retrieval: bge-m3 dense + sparse (score = dense + 0.3 * sparse) over chunk text with the
context header. Two modes:
  open    search everything
  routed  rule-based filters from the question (products, agency, guidance; ra_chunk.query).
          Each detected product is searched separately and gets its own k results.

Metrics per variant and mode, over evidence groups that are located (pending ones skipped):
  recall@k         groups whose evidence quote is inside a retrieved chunk
  ctx_recall@k     ... inside a retrieved chunk or its parent (what the answer step reads)
  full@k           questions with every group found (ctx)
  ctx_tokens@k     tokens the answer step would read (unique parents, or the chunk itself)
  other_product@k  share of retrieved product chunks from a product the question did not name

  python eval/retrieval/compare_chunking.py                 # all variants, writes results.md
  python eval/retrieval/compare_chunking.py --variants V2 V3 --k 5 10
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ra_chunk.chunker import ChunkConfig, chunk_document  # noqa: E402
from ra_chunk.embed import BgeM3, sparse_score  # noqa: E402
from ra_chunk.query import Router  # noqa: E402

VARIANTS = {
    "V1": dict(narrative="block", rows=False),
    "V2": dict(rows=False),
    "V3": dict(),
    "V4": dict(target=800, max_tokens=1000),
}
W_SPARSE = 0.3


def norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).lower().split())


def load_docs(db: str):
    """Parsed documents (latest ok parse per document) with their metadata."""
    from ra_ingest.db import DocumentRow
    from ra_ingest.metadata import build_document_metadata
    from ra_prep.cli import _store
    from ra_prep.store import PrepDocumentRow
    from sqlalchemy import select

    out = []
    st = _store(db)
    with st.session() as s:
        rows = s.scalars(select(PrepDocumentRow).where(PrepDocumentRow.status == "ok"))
        seen = set()
        for r in sorted(rows, key=lambda r: r.engine, reverse=True):
            if r.document_id in seen:
                continue
            seen.add(r.document_id)
            meta = build_document_metadata(s, s.get(DocumentRow, r.document_id))
            out.append((json.loads(Path(r.output_path).read_text(encoding="utf-8")), meta))
    return out


def build(variant: str, docs, count) -> tuple[list[dict], dict]:
    from ra_ingest.metadata import context_header

    cfg = ChunkConfig(count=count, **VARIANTS[variant])
    chunks, parents = [], {}
    for doc, meta in docs:
        ch, par = chunk_document(json.loads(json.dumps(doc)), cfg)
        parents.update({k: v.text for k, v in par.items()})
        for c in ch:
            m = c.meta()
            if c.kind == "row":  # the line already carries caption and column names
                m = {"study_ids": c.study_ids, "speaker": c.speaker}
            chunks.append({
                "id": c.id, "kind": c.kind, "doc_id": c.doc_id, "text": c.text, "parent": c.parent_id,
                "pages": c.pages, "program": meta.get("program_id"), "agency": meta.get("agency"),
                "guidance": meta.get("doc_kind") == "guidance",
                "embed": context_header(meta, m) + "\n" + c.text,
            })
    return chunks, parents


def search(q, chunks, vecs, mask, k):
    dense = vecs["dense"] @ q[0].astype(np.float32)
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    top = idx[np.argsort(-dense[idx])[: max(200, k * 20)]]  # dense shortlist, then hybrid rerank
    scored = sorted(((float(dense[i]) + W_SPARSE * sparse_score(q[1], vecs["sparse"][i]), int(i))
                     for i in top), reverse=True)
    return [i for _, i in scored[:k]]


def evaluate(gold, chunks, parents, vecs, qvecs, router, mode, k):
    prog = np.array([c["program"] or "" for c in chunks])
    agency = np.array([c["agency"] for c in chunks])
    guid = np.array([c["guidance"] for c in chunks])
    allm = np.ones(len(chunks), bool)
    res = {"groups": 0, "hit": 0, "ctx": 0, "full": 0, "questions": 0, "tokens": [], "other": [0, 0],
           "per_q": {}}
    for q in gold["questions"]:
        groups = [g for g in q.get("evidence", []) if "any" in g]
        r = router.route(q["question"])
        if mode == "open" or not (r.products or r.agency or r.guidance):
            hits = search(qvecs[q["id"]], chunks, vecs, allm, k)
        else:
            base = allm.copy()
            if r.agency:
                base &= agency == r.agency
            if r.guidance:
                base &= guid
            if r.products:
                hits = []
                for p in r.products:
                    hits += search(qvecs[q["id"]], chunks, vecs, base & (prog == p), k)
            else:
                hits = search(qvecs[q["id"]], chunks, vecs, base, k)
        named = set(r.products) | set(q.get("products") or [])
        for i in hits:
            if chunks[i]["program"] and named:
                res["other"][1] += 1
                res["other"][0] += chunks[i]["program"] not in named
        seen_par, toks = set(), 0
        for i in hits:
            key = chunks[i]["parent"] or chunks[i]["id"]
            if key not in seen_par:
                seen_par.add(key)
                toks += len(parents.get(chunks[i]["parent"], chunks[i]["text"])) // 4
        if not groups:
            continue
        res["questions"] += 1
        res["tokens"].append(toks)
        found_all = True
        q_detail = []
        for g in groups:
            res["groups"] += 1
            want = [(e["doc"], norm(e["quote"])) for e in g["any"]]
            hit = any(chunks[i]["doc_id"] == d and qq in norm(chunks[i]["text"]) for i in hits for d, qq in want)
            ctx = hit or any(chunks[i]["doc_id"] == d and chunks[i]["parent"]
                             and qq in norm(parents[chunks[i]["parent"]]) for i in hits for d, qq in want)
            res["hit"] += hit
            res["ctx"] += ctx
            found_all &= ctx
            q_detail.append(f"{g['group']}:{'O' if hit else ('P' if ctx else 'x')}")
        res["full"] += found_all
        res["per_q"][q["id"]] = " ".join(q_detail)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=f"sqlite:///{ROOT / 'data' / 'ra.db'}")
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS))
    ap.add_argument("--k", nargs="+", type=int, default=[5, 10])
    ap.add_argument("--cache", default=str(ROOT / "data" / "emb_cache" / "bge-m3.pkl"))
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--out", default=str(ROOT / "eval" / "retrieval" / "results"))
    a = ap.parse_args()

    gold = yaml.safe_load((ROOT / "eval" / "retrieval" / "questions.yaml").read_text(encoding="utf-8"))
    router = Router(ROOT / "config" / "products.yaml")
    docs = load_docs(a.db)
    print(f"{len(docs)} parsed documents", flush=True)
    model = BgeM3(cache=a.cache, threads=a.threads)

    built = {}
    for v in a.variants:
        chunks, parents = build(v, docs, model.count_tokens)
        built[v] = (chunks, parents)
        kinds = {}
        for c in chunks:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        print(v, len(chunks), "chunks", kinds, flush=True)
    texts = sorted({c["embed"] for v in built for c in built[v][0]})
    todo = [t for t in texts if model.key(t) not in model.cache]
    print(f"embedding {len(todo)} new of {len(texts)} unique texts "
          f"(~{sum(len(t) for t in todo) // 4} tokens)", flush=True)
    t0 = time.time()
    model.encode(texts, log=lambda m: print(m, f"{time.time() - t0:.0f}s", flush=True))
    qtexts = {q["id"]: q["question"] for q in gold["questions"]}
    qv = model.encode(list(qtexts.values()))
    qvecs = dict(zip(qtexts, qv))

    rows, detail = [], {}
    for v in a.variants:
        chunks, parents = built[v]
        enc = model.encode([c["embed"] for c in chunks])
        vecs = {"dense": np.stack([e[0] for e in enc]).astype(np.float32), "sparse": [e[1] for e in enc]}
        for mode in ("open", "routed"):
            for k in a.k:
                r = evaluate(gold, chunks, parents, vecs, qvecs, router, mode, k)
                rows.append((v, mode, k, r))
                detail[f"{v} {mode} @{k}"] = r["per_q"]

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["| variant | mode | k | recall | ctx_recall | full (ctx) | ctx_tokens (median) | other_product |",
             "|---|---|---|---|---|---|---|---|"]
    for v, mode, k, r in rows:
        oth = f"{100 * r['other'][0] / r['other'][1]:.0f}%" if r["other"][1] else "-"
        lines.append(f"| {v} | {mode} | {k} | {100 * r['hit'] / r['groups']:.1f}% | "
                     f"{100 * r['ctx'] / r['groups']:.1f}% | {r['full']}/{r['questions']} | "
                     f"{int(np.median(r['tokens']))} | {oth} |")
    table = "\n".join(lines)
    print(table)
    (out / "summary.md").write_text(table + "\n", encoding="utf-8")
    (out / "per_question.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
