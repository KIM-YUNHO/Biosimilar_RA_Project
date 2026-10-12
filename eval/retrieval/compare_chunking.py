"""Chunking comparison on the retrieval gold set (eval/retrieval/questions.yaml).

Variants (docs/chunking-strategy.md 6):
  V1  one parsed block = one chunk, whole tables, no parents
  V2  section chunks of ~400 tokens, whole tables, parents
  V3  V2 + table row chunks
  V4  section chunks of ~800 tokens, whole tables + row chunks, parents
  V2n V2 with the chunk text alone embedded (no context header)
  V2m V2 with leaf sections under 120 tokens merged under their parent section

Retrieval: bge-m3 dense + sparse (score = dense + 0.3 * sparse) over chunk text with the
context header. Query forms:
  ko      the Korean question as asked
  ko+gl   Korean question + English RA terms from config/query_glossary.yaml (rule-based)
  en      a human English translation (question_en; reference only, shows the language gap)
Two modes:
  open    search everything
  routed  rule-based filters from the question (products, agency, guidance; ra_chunk.query).
          Each detected product is searched separately and gets its own k results.
  routed_sub  as routed, but each product's search uses a sub-query naming only that product

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

from ra_chunk.chunker import ChunkConfig  # noqa: E402
from ra_chunk.embed import BgeM3  # noqa: E402
from ra_chunk.index import build_chunks, embed_text  # noqa: E402
from ra_chunk.query import Glossary, Router, product_subquery  # noqa: E402
from ra_chunk.search import W_SPARSE, hybrid_search, search_plan  # noqa: E402

VARIANTS = {
    "V1": dict(narrative="block", rows=False),
    "V2": dict(rows=False),
    "V3": dict(),
    "V4": dict(target=800, max_tokens=1000),
    "V2n": dict(rows=False, header=False),   # V2 without the context header in the embedded text
    "V2m": dict(rows=False, min_section=120),  # V2 + short sibling sections merged under the parent
}


def norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).lower().split())


def load_docs(db: str, only: set[int] | None = None, store_url: str | None = None):
    """Parsed documents (latest ok parse per document) with their metadata."""
    from ra_ingest.db import DocumentRow
    from ra_ingest.metadata import build_document_metadata
    from ra_ingest.objstore import open_store
    from ra_prep.cli import _store
    from ra_prep.store import PrepDocumentRow
    from sqlalchemy import select

    out = []
    objects = open_store(store_url)
    st = _store(db)
    with st.session() as s:
        rows = s.scalars(select(PrepDocumentRow).where(PrepDocumentRow.status == "ok"))
        seen = set()
        for r in sorted(rows, key=lambda r: r.engine, reverse=True):
            if r.document_id in seen or (only is not None and r.document_id not in only):
                continue
            seen.add(r.document_id)
            meta = build_document_metadata(s, s.get(DocumentRow, r.document_id))
            out.append((json.loads(objects.read_text(r.output_path)), meta))
    return out


def build(variant: str, docs, count) -> tuple[list[dict], dict]:
    """Chunks of one variant (same chunking and embedded text as ra_chunk.index for V2)."""
    opts = dict(VARIANTS[variant])
    header = opts.pop("header", True)
    cfg = ChunkConfig(count=count, **opts)
    chunks, parents = [], {}
    for doc, meta in docs:
        ch, par = build_chunks(doc, cfg)
        parents.update({k: v.text for k, v in par.items()})
        for c in ch:
            chunks.append({
                "id": c.id, "kind": c.kind, "doc_id": c.doc_id, "text": c.text, "parent": c.parent_id,
                "pages": c.pages, "program": meta.get("program_id"), "agency": meta.get("agency"),
                "doc_type": meta.get("doc_type"), "section_path": c.section_path,
                "guidance": meta.get("doc_kind") == "guidance",
                "embed": embed_text(c, meta, header),
            })
    return chunks, parents


def search(q, chunks, vecs, mask, k, w_sparse=W_SPARSE):
    """Dense shortlist, then dense + w * sparse (ra_chunk.search.hybrid_search)."""
    return hybrid_search(q, vecs["dense"], vecs["sparse"], mask, k, w_sparse)


def chunk_columns(chunks: list[dict]) -> dict:
    """Filter columns of in-memory chunks (ra_chunk.search.Filters.mask)."""
    return {"program": np.array([c["program"] or "" for c in chunks], dtype=object),
            "agency": np.array([c["agency"] for c in chunks], dtype=object),
            "guidance": np.array([c["guidance"] for c in chunks], dtype=bool),
            "doc_type": np.array([c.get("doc_type") or "" for c in chunks], dtype=object),
            "doc": np.array([c["doc_id"] for c in chunks]),
            "section_path": [c.get("section_path") or [] for c in chunks]}


def evaluate(gold, chunks, parents, vecs, qvecs, router, mode, k, w_sparse=W_SPARSE, searcher=None,
             doc_type_search=True, label_sections=True):
    """qvecs: question id (or (id, product) for sub-queries) -> query vectors (one query form).
    searcher(key, qv, mask, k, w_sparse) replaces the first-stage search (e.g. with re-ranking)."""
    def run(key, qv, mask):
        if searcher is not None:
            return searcher(key, qv, mask, k, w_sparse)
        return search(qv, chunks, vecs, mask, k, w_sparse)

    cols = chunk_columns(chunks)
    res = {"groups": 0, "hit": 0, "ctx": 0, "full": 0, "questions": 0, "tokens": [], "other": [0, 0],
           "per_q": {}}
    for q in gold["questions"]:
        groups = [g for g in q.get("evidence", []) if "any" in g]
        r = router.route(q["question"])
        hits = []
        for ps in search_plan(r, router.references, mode, doc_type_search, label_sections):  # = ra_chunk.retrieve
            p = ps.product
            key = (q["id"], p) if p and mode == "routed_sub" and len(r.products) > 1 else q["id"]
            found = run(key, qvecs[key], ps.filters.mask(cols))
            if ps.types is not None:  # also search the document types the question names
                found += [i for i in run(key, qvecs[key], ps.types.mask(cols)) if i not in found]
            hits += [i for i in found if i not in hits]
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
    ap.add_argument("--corpus", default=str(ROOT / "eval" / "retrieval" / "corpus_docs.txt"),
                    help="document ids to search (fixed corpus); 'all' for every parsed document")
    ap.add_argument("--queries", nargs="+", default=["ko", "ko+gl", "en"])
    ap.add_argument("--no-doc-types", action="store_true", help="routing without the document-type search")
    ap.add_argument("--no-label-sections", action="store_true", help="document-type search without label sections")
    ap.add_argument("--store", help="object store (default: RA_STORE or data/)")
    ap.add_argument("--cache", default=str(ROOT / "data" / "emb_cache" / "bge-m3.pkl"))
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--out", default=str(ROOT / "eval" / "retrieval" / "results"))
    a = ap.parse_args()

    gold = yaml.safe_load((ROOT / "eval" / "retrieval" / "questions.yaml").read_text(encoding="utf-8"))
    router = Router(ROOT / "config" / "products.yaml")
    only = None
    if a.corpus != "all":
        only = {int(x) for x in Path(a.corpus).read_text().split() if x.isdigit()}
    docs = load_docs(a.db, only, a.store)
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
    glossary = Glossary(ROOT / "config" / "query_glossary.yaml")
    forms = {"ko": lambda q: q["question"], "ko+gl": lambda q: glossary.expand(q["question"]),
             "en": lambda q: q["question_en"]}
    qvecs = {}
    for f in a.queries:
        qtexts = {q["id"]: forms[f](q) for q in gold["questions"]}
        for q in gold["questions"]:
            prods = router.route(q["question"]).products
            if len(prods) > 1:
                for p in prods:
                    qtexts[(q["id"], p)] = product_subquery(qtexts[q["id"]], router, p, router.display)
        qvecs[f] = dict(zip(qtexts, model.encode(list(qtexts.values()))))

    rows, detail = [], {}
    for v in a.variants:
        chunks, parents = built[v]
        enc = model.encode([c["embed"] for c in chunks])
        vecs = {"dense": np.stack([e[0] for e in enc]).astype(np.float32), "sparse": [e[1] for e in enc]}
        for f in a.queries:
            for mode in ("open", "routed", "routed_sub"):
                for k in a.k:
                    for score, w in (("hybrid", W_SPARSE), ("dense", 0.0)):
                        r = evaluate(gold, chunks, parents, vecs, qvecs[f], router, mode, k, w,
                                     doc_type_search=not a.no_doc_types,
                                     label_sections=not a.no_label_sections)
                        rows.append((v, f, mode, k, score, r))
                        detail[f"{v} {f} {mode} @{k} {score}"] = r["per_q"]

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["| variant | query | mode | k | score | recall | ctx_recall | full (ctx) | ctx_tokens (median) | other_product |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for v, f, mode, k, score, r in rows:
        oth = f"{100 * r['other'][0] / r['other'][1]:.0f}%" if r["other"][1] else "-"
        lines.append(f"| {v} | {f} | {mode} | {k} | {score} | {100 * r['hit'] / r['groups']:.1f}% | "
                     f"{100 * r['ctx'] / r['groups']:.1f}% | {r['full']}/{r['questions']} | "
                     f"{int(np.median(r['tokens']))} | {oth} |")
    table = "\n".join(lines)
    print(table)
    tag = (Path(a.corpus).stem if a.corpus != "all" else "all") + ("_nodt" if a.no_doc_types else "") + ("_nols" if a.no_label_sections else "")
    name = f"summary_{'_'.join(a.variants)}_{tag}"
    (out / f"{name}.md").write_text(table + "\n", encoding="utf-8")
    (out / f"per_question_{'_'.join(a.variants)}_{tag}.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
