"""Question -> chunks from the index, the query flow decided in docs/chunking-strategy.md:

  rule routing (products, agency, guidance) -> Korean RA glossary expansion -> hybrid search,
  one search per named product -> optional cross-encoder re-ranking -> parents for the answer

Same steps as the evaluation (eval/retrieval/compare_chunking.py, rerank_test.py), so the
measured recall applies to what the answer step receives.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from .query import Glossary, Router, product_subquery
from .search import W_SPARSE, ChunkIndex, Filters, Hit
from .store import IndexChunkRow


@dataclass
class Retrieved:
    chunk: IndexChunkRow
    score: float
    parent_text: str        # what the answer step reads (whole section / table, or the chunk)
    product: str | None     # the routed product this hit was searched for


class Retriever:
    def __init__(self, s: Session, embedder, router: Router, glossary: Glossary | None = None,
                 reranker=None, depth: int = 30, w_sparse: float = W_SPARSE, subqueries: bool = False):
        self.index = ChunkIndex(s, embedder.name)
        self.embedder, self.router, self.glossary = embedder, router, glossary
        self.reranker, self.depth, self.w = reranker, depth, w_sparse
        self.subqueries = subqueries
        self._qv: dict[str, tuple] = {}

    def _vec(self, text: str):
        if text not in self._qv:
            self._qv[text] = self.embedder.encode([text])[0]
        return self._qv[text]

    def search(self, qtext: str, f: Filters, k: int) -> list[Hit]:
        qv = self._vec(qtext)
        if self.reranker is None:
            return self.index.search(qv, f, k, self.w)
        cand = self.index.search(qv, f, self.depth, self.w)
        if not cand:
            return []
        rows = self.index.chunks([h.chunk_id for h in cand])
        scores = self.reranker.score(qtext, [rows[h.chunk_id].embed_text for h in cand])
        order = sorted(range(len(cand)), key=lambda j: -scores[j])
        return [Hit(cand[j].chunk_id, scores[j]) for j in order[:k]]

    def retrieve(self, question: str, k: int = 10, mode: str = "routed",
                 document_ids: list[int] | None = None) -> list[Retrieved]:
        """mode: "routed" (filters from the question; each product searched separately) or "open"."""
        qtext = self.glossary.expand(question) if self.glossary else question
        r = self.router.route(question)
        base = Filters(document_ids=document_ids)
        found: list[tuple[Hit, str | None]] = []
        if mode == "open" or not (r.products or r.agency or r.guidance):
            found = [(h, None) for h in self.search(qtext, base, k)]
        else:
            base = replace(base, agency=r.agency, guidance=r.guidance)
            if r.products:
                for p in r.products:
                    qt = qtext
                    if self.subqueries and len(r.products) > 1:
                        qt = product_subquery(qtext, self.router, p, self.router.display)
                    found += [(h, p) for h in self.search(qt, replace(base, program_id=p), k)]
            else:
                found = [(h, None) for h in self.search(qtext, base, k)]
        rows = self.index.chunks([h.chunk_id for h, _ in found])
        parents = self.index.parents(list(rows.values()))
        out = []
        for h, p in found:
            c = rows[h.chunk_id]
            par = parents.get((c.index_document_id, c.parent_key))
            out.append(Retrieved(c, h.score, par.text if par else c.text, p))
        return out
