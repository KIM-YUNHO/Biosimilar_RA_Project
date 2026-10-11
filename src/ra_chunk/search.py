"""Hybrid search over indexed chunks: bge-m3 dense shortlist, then dense + w * sparse.

The same two-stage scoring is used by the evaluation scripts (eval/retrieval) and by the
index, so results measured there carry over:
  1. shortlist: the max(200, 20k) chunks with the highest dense inner product
  2. score = dense + w_sparse * sparse(lexical) over the shortlist, top k

ChunkIndex runs it in SQL on PostgreSQL (pgvector halfvec / sparsevec) and in numpy on SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import Float, and_, bindparam, select
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
                                         c.document_id)
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
            }
        return self._mem

    def _search_mem(self, q, f: Filters, k: int, w: float) -> list[Hit]:
        m = self._load()
        mask = np.ones(len(m["id"]), bool)
        if f.program_id:
            mask &= m["program"] == f.program_id
        if f.agency:
            mask &= m["agency"] == f.agency
        if f.guidance:
            mask &= m["guidance"]
        if f.document_ids is not None:
            mask &= np.isin(m["doc"], list(f.document_ids))
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
