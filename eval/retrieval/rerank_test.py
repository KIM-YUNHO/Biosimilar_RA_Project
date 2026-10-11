"""Re-ranking test: first-stage hybrid search (bge-m3) for `depth` candidates, then the
cross-encoder bge-reranker-v2-m3 re-orders them and the top k are kept.

Same gold set, corpus, routing and metrics as compare_chunking.py; chunk variant V2 (the
chosen one). Compares, per query form and mode: no re-ranking vs re-ranking.

  python eval/retrieval/rerank_test.py --depth 30
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare_chunking as C  # noqa: E402
from ra_chunk.embed import BgeM3  # noqa: E402
from ra_chunk.query import Glossary, Router, product_subquery  # noqa: E402
from ra_chunk.rerank import Reranker  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=f"sqlite:///{ROOT / 'data' / 'ra.db'}")
    ap.add_argument("--variant", default="V2")
    ap.add_argument("--depth", type=int, default=30, help="first-stage candidates per search")
    ap.add_argument("--k", nargs="+", type=int, default=[5, 10])
    ap.add_argument("--queries", nargs="+", default=["ko", "ko+gl"])
    ap.add_argument("--modes", nargs="+", default=["routed", "routed_sub"])
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--out", default=str(ROOT / "eval" / "retrieval" / "results"))
    a = ap.parse_args()

    gold = yaml.safe_load((ROOT / "eval" / "retrieval" / "questions.yaml").read_text(encoding="utf-8"))
    router = Router(ROOT / "config" / "products.yaml")
    glossary = Glossary(ROOT / "config" / "query_glossary.yaml")
    only = {int(x) for x in (ROOT / "eval" / "retrieval" / "corpus_docs.txt").read_text().split() if x.isdigit()}
    docs = C.load_docs(a.db, only)
    emb = BgeM3(cache=ROOT / "data" / "emb_cache" / "bge-m3.pkl", threads=a.threads)
    chunks, parents = C.build(a.variant, docs, emb.count_tokens)
    enc = emb.encode([c["embed"] for c in chunks])
    vecs = {"dense": np.stack([e[0] for e in enc]).astype(np.float32), "sparse": [e[1] for e in enc]}
    rr = Reranker(cache=ROOT / "data" / "emb_cache" / "rerank.pkl", threads=a.threads)
    print(f"{len(docs)} documents, {len(chunks)} chunks ({a.variant})", flush=True)

    forms = {"ko": lambda q: q["question"], "ko+gl": lambda q: glossary.expand(q["question"])}
    rows = []
    for f in a.queries:
        texts = {}
        for q in gold["questions"]:
            texts[q["id"]] = forms[f](q)
            prods = router.route(q["question"]).products
            if len(prods) > 1:
                for p in prods:
                    texts[(q["id"], p)] = product_subquery(texts[q["id"]], router, p, router.display)
        qvecs = dict(zip(texts, emb.encode(list(texts.values()))))

        def reranking(key, qv, mask, k, w):
            cand = C.search(qv, chunks, vecs, mask, a.depth, w)
            if not cand:
                return []
            scores = rr.score(texts[key], [chunks[i]["embed"] for i in cand])
            order = sorted(range(len(cand)), key=lambda j: -scores[j])
            return [cand[j] for j in order[:k]]

        for mode in a.modes:
            for k in a.k:
                for name, searcher in (("hybrid", None), (f"hybrid@{a.depth}+rerank", reranking)):
                    t0 = time.time()
                    r = C.evaluate(gold, chunks, parents, vecs, qvecs, router, mode, k, C.W_SPARSE, searcher)
                    rows.append((f, mode, k, name, r, time.time() - t0))
                    print(f, mode, k, name, f"{100 * r['ctx'] / r['groups']:.1f}%", flush=True)
                    rr.save()

    lines = ["| query | mode | k | ranking | recall | ctx_recall | full (ctx) | ctx_tokens (median) |",
             "|---|---|---|---|---|---|---|---|"]
    for f, mode, k, name, r, _ in rows:
        lines.append(f"| {f} | {mode} | {k} | {name} | {100 * r['hit'] / r['groups']:.1f}% | "
                     f"{100 * r['ctx'] / r['groups']:.1f}% | {r['full']}/{r['questions']} | "
                     f"{int(np.median(r['tokens']))} |")
    n_search = sum(1 for _ in rr.cache)
    lines.append("")
    lines.append(f"re-ranker: {rr.computed} pairs scored in {rr.seconds:.0f} s "
                 f"({rr.seconds / max(rr.computed, 1):.2f} s/pair on this CPU; cache holds {n_search} pairs)")
    table = "\n".join(lines)
    print(table)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"rerank_{a.variant}_depth{a.depth}.md").write_text(table + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
