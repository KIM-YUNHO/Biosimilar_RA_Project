"""Indexing stage: parsed documents -> chunks -> bge-m3 vectors -> index tables.

One document at a time, so a stopped run keeps what it finished and a scheduler can run
documents as separate tasks. A document is skipped when its index already matches

    (document, version sha256, prep engine, chunker, embedding model, document-metadata hash)

and anything else (a new document version, a re-parse, a chunker or model change, changed
metadata such as a guidance status that goes into the context header) indexes it again and
retires the previous index of that document for the same model. Vectors of chunk texts that
were embedded before (same text, same model) are copied instead of recomputed.

Which documents: the latest version of each document that has an ok parse. A document whose
latest version was skipped by preprocessing (superseded label, near-duplicate) has its index
retired.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ra_ingest.db import DocumentRow, DocumentVersionRow, utcnow
from ra_ingest.metadata import build_document_metadata, context_header
from ra_ingest.objstore import ObjectStore, open_store

from . import CHUNKER_VERSION
from .chunker import Chunk, ChunkConfig, Parent, chunk_document
from .store import IndexChunkRow, IndexDocumentRow, IndexParentRow

# The chunk setup chosen in docs/chunking-strategy.md 7.3 (V2): section chunks of ~400
# tokens (max 600), whole tables, no row chunks, parents, context header.
INDEX_VARIANT = "V2"
INDEX_CHUNKING = dict(rows=False)
CHUNKER_ID = f"{CHUNKER_VERSION}:{INDEX_VARIANT}"

# metadata fields that do not change what is indexed
_META_VOLATILE = {"retrieved_at", "storage_path", "content_type"}


class Embedder(Protocol):
    name: str

    def count_tokens(self, text: str) -> int: ...

    def encode(self, texts: list[str]) -> list[tuple[np.ndarray, dict[int, float]]]: ...


def embed_text(c: Chunk, meta: dict, header: bool = True) -> str:
    """Text that is embedded for a chunk: context header + chunk text."""
    if not header:
        return c.text
    m = c.meta()
    if c.kind == "row":  # the line already carries caption and column names
        m = {"study_ids": c.study_ids, "speaker": c.speaker}
    return context_header(meta, m) + "\n" + c.text


def build_chunks(doc: dict, cfg: ChunkConfig) -> tuple[list[Chunk], dict[str, Parent]]:
    return chunk_document(copy.deepcopy(doc), cfg)  # the chunker marks blocks it attaches


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def meta_hash(meta: dict) -> str:
    keep = {k: v for k, v in meta.items() if k not in _META_VOLATILE}
    return text_hash(json.dumps(keep, sort_keys=True, ensure_ascii=False, default=str))


@dataclass
class Candidate:
    document_id: int
    sha256: str | None
    action: str                     # index | retire | not_parsed
    prep_engine: str | None = None
    output_path: str | None = None
    reason: str = ""


def candidates(s: Session, program_ids: list[str] | None = None, agencies: list[str] | None = None,
               document_ids: list[int] | None = None, guidance: bool = False) -> list[Candidate]:
    from ra_prep import ENGINE_VERSION
    from ra_prep.store import PrepDocumentRow

    q = select(DocumentRow).where(DocumentRow.fetch_status == "fetched")
    if program_ids:
        q = q.where(DocumentRow.program_id.in_(program_ids))
    if agencies:
        q = q.where(DocumentRow.agency.in_(agencies))
    if document_ids:
        q = q.where(DocumentRow.id.in_(document_ids))
    if guidance:
        q = q.where(DocumentRow.doc_kind == "guidance")
    out = []
    for d in s.scalars(q.order_by(DocumentRow.id)):
        v = s.scalar(select(DocumentVersionRow).where(DocumentVersionRow.document_id == d.id)
                     .order_by(DocumentVersionRow.retrieved_at.desc(), DocumentVersionRow.id.desc()))
        if v is None:
            continue
        rows = list(s.scalars(select(PrepDocumentRow).where(PrepDocumentRow.sha256 == v.sha256)))
        ok = [r for r in rows if r.status == "ok"]
        if ok:  # the current engine's parse if there is one, else the newest
            r = max(ok, key=lambda r: (r.engine.startswith(ENGINE_VERSION + "+"), r.id))
            out.append(Candidate(d.id, v.sha256, "index", r.engine, r.output_path))
        elif any(r.status == "skipped" for r in rows):
            why = next(r.reason for r in rows if r.status == "skipped")
            out.append(Candidate(d.id, v.sha256, "retire", reason=f"prep skipped: {why}"))
        else:
            out.append(Candidate(d.id, v.sha256, "not_parsed",
                                 reason="failed" if rows else "not preprocessed yet"))
    return out


def retire(s: Session, document_id: int, embed_model: str, reason: str,
           keep: int | None = None) -> int:
    """Retire the active index of a document for one model (except index document `keep`)."""
    q = select(IndexDocumentRow).where(IndexDocumentRow.document_id == document_id,
                                       IndexDocumentRow.embed_model == embed_model,
                                       IndexDocumentRow.retired_at.is_(None))
    n = 0
    for row in s.scalars(q):
        if row.id == keep:
            continue
        row.retired_at, row.retired_reason = utcnow(), reason[:256]
        s.execute(update(IndexChunkRow).where(IndexChunkRow.index_document_id == row.id)
                  .values(active=False))
        n += 1
    return n


def _reuse_vectors(s: Session, model: str, hashes: list[str]) -> dict[str, tuple]:
    found: dict[str, tuple] = {}
    todo = list(dict.fromkeys(hashes))
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        for h, dense, sparse in s.execute(
                select(IndexChunkRow.embed_hash, IndexChunkRow.dense, IndexChunkRow.sparse)
                .where(IndexChunkRow.embed_model == model, IndexChunkRow.embed_hash.in_(part))):
            found.setdefault(h, (dense, sparse))
    return found


@dataclass
class IndexResult:
    indexed: int = 0
    cached: int = 0
    reactivated: int = 0
    retired: int = 0
    not_parsed: int = 0
    failed: int = 0
    chunks: int = 0
    embedded: int = 0               # vectors computed (the rest were copied from earlier chunks)
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)


def index_document(s: Session, cand: Candidate, embedder: Embedder, store: ObjectStore,
                   cfg: ChunkConfig | None = None, force: bool = False) -> tuple[str, int, int]:
    """Index one candidate. Returns (outcome, chunks, vectors computed)."""
    d = s.get(DocumentRow, cand.document_id)
    meta = build_document_metadata(s, d)
    mh = meta_hash(meta)
    key = dict(document_id=d.id, sha256=cand.sha256, prep_engine=cand.prep_engine, chunker=CHUNKER_ID,
               embed_model=embedder.name, meta_hash=mh)
    existing = s.scalar(select(IndexDocumentRow).filter_by(**key))
    if existing is not None and existing.status == "ok" and not force:
        if existing.retired_at is None:
            return "cached", existing.n_chunks, 0
        existing.retired_at = existing.retired_reason = None
        s.execute(update(IndexChunkRow).where(IndexChunkRow.index_document_id == existing.id)
                  .values(active=True))
        retire(s, d.id, embedder.name, f"replaced by index_document {existing.id}", keep=existing.id)
        s.commit()
        return "reactivated", existing.n_chunks, 0

    t0 = time.time()
    doc = json.loads(store.read_text(cand.output_path))
    cfg = cfg or ChunkConfig(count=embedder.count_tokens, **INDEX_CHUNKING)
    chunks, parents = build_chunks(doc, cfg)
    texts = [embed_text(c, meta) for c in chunks]
    hashes = [text_hash(t) for t in texts]
    vectors = _reuse_vectors(s, embedder.name, hashes)  # before a forced re-index drops the old rows
    todo = [t for t, h in dict(zip(texts, hashes)).items() if h not in vectors]
    for t, v in zip(todo, embedder.encode(todo) if todo else []):
        vectors[text_hash(t)] = v
    if existing is not None:
        s.query(IndexChunkRow).filter_by(index_document_id=existing.id).delete()
        s.query(IndexParentRow).filter_by(index_document_id=existing.id).delete()
        s.delete(existing)
        s.flush()

    row = IndexDocumentRow(**key, status="ok", n_chunks=len(chunks), n_embedded=len(todo))
    s.add(row)
    s.flush()
    filt = dict(program_id=meta.get("program_id"), agency=meta.get("agency"),
                jurisdiction=meta.get("jurisdiction"), doc_kind=meta.get("doc_kind"),
                doc_type=meta.get("doc_type"), decision_date=meta.get("decision_date"),
                guidance_status=meta.get("guidance_status"),
                guidance_effective_from=meta.get("guidance_effective_from"),
                guidance_effective_to=meta.get("guidance_effective_to"))
    s.add_all(IndexChunkRow(
        index_document_id=row.id, document_id=d.id, chunk_key=c.id, kind=c.kind, text=c.text,
        embed_text=t, embed_hash=h, parent_key=c.parent_id, pages=c.pages, section_path=c.section_path,
        speaker=c.speaker, table_caption=c.table_caption, study_ids=c.study_ids,
        needs_review=c.needs_review, guidance_section_id=c.guidance_section_id,
        embed_model=embedder.name, active=True, dense=vectors[h][0], sparse=vectors[h][1], **filt)
        for c, t, h in zip(chunks, texts, hashes))
    s.add_all(IndexParentRow(index_document_id=row.id, parent_key=p.id, text=p.text, pages=p.pages)
              for p in parents.values())
    retire(s, d.id, embedder.name, f"replaced by index_document {row.id}", keep=row.id)
    row.seconds = time.time() - t0
    s.commit()
    return "indexed", len(chunks), len(todo)


def run(s: Session, cands: list[Candidate], embedder: Embedder, store: ObjectStore | None = None,
        force: bool = False, log: Callable[[str], None] = print) -> IndexResult:
    store = store or open_store()
    res = IndexResult()
    t0 = time.time()
    for c in cands:
        if c.action == "retire":
            n = retire(s, c.document_id, embedder.name, c.reason)
            s.commit()
            res.retired += n
            continue
        if c.action == "not_parsed":
            res.not_parsed += 1
            continue
        try:
            outcome, n_chunks, n_emb = index_document(s, c, embedder, store, force=force)
        except Exception as e:  # recorded, the run goes on
            s.rollback()
            res.failed += 1
            msg = f"{type(e).__name__}: {e}"
            res.failures.append(f"doc {c.document_id}: {msg}")
            key = dict(document_id=c.document_id, sha256=c.sha256, prep_engine=c.prep_engine,
                       chunker=CHUNKER_ID, embed_model=embedder.name, meta_hash="-")
            if s.scalar(select(IndexDocumentRow).filter_by(**key)) is None:
                s.add(IndexDocumentRow(**key, status="failed", reason=msg[:2000]))
                s.commit()
            log(f"FAILED doc {c.document_id}: {msg}")
            continue
        setattr(res, outcome, getattr(res, outcome) + 1)
        res.chunks += n_chunks
        res.embedded += n_emb
        if outcome == "indexed":
            log(f"doc {c.document_id:>5}: {n_chunks} chunks, {n_emb} embedded")
    res.seconds = time.time() - t0
    return res
