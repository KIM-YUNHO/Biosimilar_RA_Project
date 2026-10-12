"""Hybrid search over indexed chunks: bge-m3 dense shortlist, then dense + w * sparse.

The same two-stage scoring is used by the evaluation scripts (eval/retrieval) and by the
index, so results measured there carry over:
  1. shortlist: the max(200, 20k) chunks with the highest dense inner product
  2. score = dense + w_sparse * sparse(lexical) over the shortlist, top k

ChunkIndex runs it in SQL on PostgreSQL (pgvector halfvec / sparsevec) and in numpy on SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from sqlalchemy import Float, and_, bindparam, or_, select
from sqlalchemy.orm import Session

from .store import Dense, IndexChunkRow, IndexParentRow, Sparse

W_SPARSE = 0.3


def sparse_score(q: dict[int, float], d: dict[int, float]) -> float:
    if len(q) > len(d):
        q, d = d, q
    return sum(w * d[t] for t, w in q.items() if t in d)


def shortlist_size(k: int) -> int:
    return max(200, k * 20)


def hybrid_search(q, dense: np.ndarray, sparse: list[dict], mask: np.ndarray, k: int,
                  w_sparse: float = W_SPARSE) -> list[int]:
    """In-memory search. q = (dense float16 vector, sparse dict); dense is (n, dim) float32.
    Returns row indices, best first."""
    scores = dense @ np.asarray(q[0]).astype(np.float32)
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    top = idx[np.argsort(-scores[idx])[: shortlist_size(k)]]
    scored = sorted(((float(scores[i]) + w_sparse * sparse_score(q[1], sparse[i]), int(i))
                     for i in top), reverse=True)
    return [i for _, i in scored[:k]]


@dataclass
class Filters:
    program_id: str | None = None
    agency: str | None = None
    guidance: bool = False
    document_ids: list[int] | None = None
    exclude_programs: list[str] | None = None   # e.g. reference products unless asked for
    doc_types: list[str] | None = None

    def mask(self, cols: dict) -> np.ndarray:
        """Boolean mask over in-memory chunks; cols holds equal-length arrays "program" ("" when
        none), "agency", "guidance", "doc_type" and "doc"."""
        m = np.ones(len(cols["program"]), bool)
        if self.program_id:
            m &= cols["program"] == self.program_id
        if self.exclude_programs:
            m &= ~np.isin(cols["program"], self.exclude_programs)
        if self.agency:
            m &= cols["agency"] == self.agency
        if self.guidance:
            m &= cols["guidance"]
        if self.doc_types:
            m &= np.isin(cols["doc_type"], self.doc_types)
        if self.document_ids is not None:
            m &= np.isin(cols["doc"], list(self.document_ids))
        return m


@dataclass
class PlannedSearch:
    product: str | None             # the routed product this search is for (None: no product)
    filters: Filters
    types: Filters | None = None    # extra search within the document types the question names


def search_plan(route, references: list[str], mode: str = "routed",
                doc_type_search: bool = True) -> list[PlannedSearch]:
    """The searches one question runs (docs/chunking-strategy.md 7.3, 9, 10.4). Shared by the
    evaluation (eval/retrieval/compare_chunking.py) and the retriever, so what is measured is
    what runs.

      no routing signal (or mode "open")  one search over everything
      otherwise, within scope              reference-product documents only when the question
                                           asks for them; the named agency
        products named                     one search per product, plus a guidance search when
                                           the question asks for rules
        no product                         one search (guidance only when it asks for rules)
      each search may add a search within the document types the question names
    """
    use_types = bool(route.doc_types and doc_type_search)
    if mode == "open" or not (route.products or route.agency or route.guidance or use_types):
        return [PlannedSearch(None, Filters())]
    scope = Filters(agency=route.agency,
                    exclude_programs=None if route.reference else list(references) or None)
    typed = (lambda f: replace(f, doc_types=list(route.doc_types))) if use_types else (lambda f: None)
    if route.products:
        plan = [PlannedSearch(p, replace(scope, program_id=p), typed(replace(scope, program_id=p)))
                for p in route.products]
        if route.guidance:
            plan.append(PlannedSearch(None, replace(scope, guidance=True)))
        return plan
    return [PlannedSearch(None, replace(scope, guidance=route.guidance), typed(scope))]


@dataclass
class Hit:
    chunk_id: int
    score: float


class ChunkIndex:
    """Search over the active chunks of one embedding model."""

    def __init__(self, s: Session, embed_model: str):
        self.s = s
        self.model = embed_model
        self.pg = s.get_bind().dialect.name == "postgresql"
        self._mem = None  # SQLite: arrays loaded once

    # -- PostgreSQL ------------------------------------------------------------------------
    def _where(self, f: Filters):
        c = IndexChunkRow
        cond = [c.active.is_(True), c.embed_model == self.model]
        if f.program_id:
            cond.append(c.program_id == f.program_id)
        if f.agency:
            cond.append(c.agency == f.agency)
        if f.guidance:
            cond.append(c.doc_kind == "guidance")
        if f.document_ids is not None:
            cond.append(c.document_id.in_(f.document_ids))
        if f.exclude_programs:
            cond.append(or_(c.program_id.is_(None), c.program_id.notin_(f.exclude_programs)))
        if f.doc_types:
            cond.append(c.doc_type.in_(f.doc_types))
        return and_(*cond)

    def _search_pg(self, q, f: Filters, k: int, w: float) -> list[Hit]:
        c = IndexChunkRow
        qd = bindparam("qd", q[0], type_=Dense())
        qs = bindparam("qs", q[1], type_=Sparse())
        neg_ip = c.dense.op("<#>", return_type=Float)(qd)  # pgvector: negative inner product
        short = (select(c.id.label("id"), (-neg_ip).label("d"), c.sparse.label("sp"))
                 .where(self._where(f)).order_by(neg_ip).limit(shortlist_size(k)).subquery())
        sp_ip = -short.c.sp.op("<#>", return_type=Float)(qs)
        rows = self.s.execute(select(short.c.id, short.c.d, sp_ip)).all()
        scored = sorted(((d + w * (sp or 0.0), i) for i, d, sp in rows), reverse=True)
        return [Hit(i, sc) for sc, i in scored[:k]]

    # -- SQLite (numpy) --------------------------------------------------------------------
    def _load(self):
        if self._mem is None:
            c = IndexChunkRow
            rows = self.s.execute(select(c.id, c.dense, c.sparse, c.program_id, c.agency, c.doc_kind,
                                         c.document_id, c.doc_type)
                                  .where(c.active.is_(True), c.embed_model == self.model)
                                  .order_by(c.id)).all()
            self._mem = {
                "id": np.array([r[0] for r in rows], dtype=np.int64),
                "dense": (np.stack([r[1] for r in rows]).astype(np.float32) if rows
                          else np.zeros((0, 1), np.float32)),
                "sparse": [r[2] for r in rows],
                "program": np.array([r[3] or "" for r in rows], dtype=object),
                "agency": np.array([r[4] or "" for r in rows], dtype=object),
                "guidance": np.array([r[5] == "guidance" for r in rows], dtype=bool),
                "doc": np.array([r[6] for r in rows], dtype=np.int64),
                "doc_type": np.array([r[7] or "" for r in rows], dtype=object),
            }
        return self._mem

    def _search_mem(self, q, f: Filters, k: int, w: float) -> list[Hit]:
        m = self._load()
        mask = f.mask(m)
        dq = np.asarray(q[0]).astype(np.float32)
        out = hybrid_search(q, m["dense"], m["sparse"], mask, k, w)
        return [Hit(int(m["id"][i]), float(m["dense"][i] @ dq + w * sparse_score(q[1], m["sparse"][i])))
                for i in out]

    # -- public ----------------------------------------------------------------------------
    def search(self, q, filters: Filters | None = None, k: int = 10,
               w_sparse: float = W_SPARSE) -> list[Hit]:
        """q = (dense vector, sparse dict) from the same embedding model."""
        f = filters or Filters()
        return self._search_pg(q, f, k, w_sparse) if self.pg else self._search_mem(q, f, k, w_sparse)

    def chunks(self, ids: list[int]) -> dict[int, IndexChunkRow]:
        rows = self.s.scalars(select(IndexChunkRow).where(IndexChunkRow.id.in_(ids))).all()
        return {r.id: r for r in rows}

    def parents(self, chunks: list[IndexChunkRow]) -> dict[tuple[int, str], IndexParentRow]:
        keys = {(c.index_document_id, c.parent_key) for c in chunks if c.parent_key}
        if not keys:
            return {}
        rows = self.s.scalars(select(IndexParentRow).where(
            IndexParentRow.index_document_id.in_({k[0] for k in keys}))).all()
        return {(p.index_document_id, p.parent_key): p for p in rows
                if (p.index_document_id, p.parent_key) in keys}
