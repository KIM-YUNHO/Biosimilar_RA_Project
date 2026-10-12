"""Question -> chunks from the index, the query flow decided in docs/chunking-strategy.md:

  rule routing (products, agency, guidance, reference product, document types) -> Korean RA
  glossary expansion -> the searches of ra_chunk.search.search_plan (one per named product,
  guidance beside them when rules are asked, document-type searches) -> optional cross-encoder
  re-ranking -> parents for the answer

The evaluation (eval/retrieval/compare_chunking.py, rerank_test.py) runs the same plan, so the
measured recall applies to what the answer step receives.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from .query import Glossary, Router, product_subquery
from .search import W_SPARSE, ChunkIndex, Hit, search_plan
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
                 document_ids: list[int] | None = None, doc_type_search: bool = True) -> list[Retrieved]:
        """mode: "routed" (filters from the question; each product searched separately) or "open"."""
        qtext = self.glossary.expand(question) if self.glossary else question
        r = self.router.route(question)
        found: list[tuple[Hit, str | None]] = []
        seen: set[int] = set()
        for ps in search_plan(r, self.router.references, mode, doc_type_search):
            qt = qtext
            if ps.product and self.subqueries and len(r.products) > 1:
                qt = product_subquery(qtext, self.router, ps.product, self.router.display)
            hits = self.search(qt, replace(ps.filters, document_ids=document_ids), k)
            if ps.types is not None:  # also search the document types the question names
                ids = {h.chunk_id for h in hits}
                hits += [h for h in self.search(qt, replace(ps.types, document_ids=document_ids), k)
                         if h.chunk_id not in ids]
            for h in hits:
                if h.chunk_id not in seen:
                    seen.add(h.chunk_id)
                    found.append((h, ps.product))
        rows = self.index.chunks([h.chunk_id for h, _ in found])
        parents = self.index.parents(list(rows.values()))
        out = []
        for h, p in found:
            c = rows[h.chunk_id]
            par = parents.get((c.index_document_id, c.parent_key))
            out.append(Retrieved(c, h.score, par.text if par else c.text, p))
        return out
