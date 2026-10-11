"""Chunk index tables, created in the same database as ra-ingest (shared Base).

  index_documents  one indexing of one document version: (document, version sha256, prep
                   engine, chunker, embedding model, document-metadata hash). A newer one for
                   the same document and model retires the older (retired_at, retired_reason)
  index_chunks     retrieval chunks with their filter fields and vectors
  index_parents    what the answer step reads for a hit (whole section, whole table)

Vectors are pgvector types on PostgreSQL (dense: halfvec, the float16 the embedder keeps;
sparse: sparsevec, bge-m3 lexical weights) and plain bytes / JSON on SQLite, so the same
code runs on both. Search on SQLite happens in numpy (ra_chunk.search).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String,
                        Text, UniqueConstraint, event, text)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ra_ingest.db import Base, utcnow

# sparsevec needs a dimension; bge-m3's vocabulary is 250,002 tokens. Storage does not depend
# on it (only non-zero entries are kept), so one generous bound serves every model.
SPARSE_DIM = 1 << 20


class Dense(TypeDecorator):
    """float16 dense vector: halfvec on PostgreSQL, raw float16 bytes elsewhere."""
    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import HALFVEC

            return dialect.type_descriptor(HALFVEC())
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        a = np.asarray(value, dtype=np.float16)
        return a.astype(np.float32) if dialect.name == "postgresql" else a.tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return np.asarray(value.to_numpy() if hasattr(value, "to_numpy") else value, dtype=np.float16)
        return np.frombuffer(value, dtype=np.float16)


class Sparse(TypeDecorator):
    """{token id: weight}: sparsevec on PostgreSQL, JSON elsewhere."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import SPARSEVEC

            return dialect.type_descriptor(SPARSEVEC())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            from pgvector import SparseVector

            return SparseVector({int(k): float(v) for k, v in value.items()}, SPARSE_DIM)
        return {str(k): float(v) for k, v in value.items()}

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return dict(zip(value.indices(), value.values()))
        return {int(k): v for k, v in value.items()}


@event.listens_for(Base.metadata, "before_create")
def _vector_extension(target, connection, tables=(), **kw):
    """pgvector is needed only when the index tables are about to be created."""
    if connection.dialect.name == "postgresql" and any(t.name == "index_chunks" for t in tables):
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


class IndexDocumentRow(Base):
    __tablename__ = "index_documents"
    __table_args__ = (UniqueConstraint("document_id", "sha256", "prep_engine", "chunker",
                                       "embed_model", "meta_hash"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    prep_engine: Mapped[str] = mapped_column(String(128))
    chunker: Mapped[str] = mapped_column(String(64))
    embed_model: Mapped[str] = mapped_column(String(128), index=True)
    meta_hash: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))                 # ok | failed
    reason: Mapped[str | None] = mapped_column(Text)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
    n_embedded: Mapped[int] = mapped_column(Integer, default=0)     # vectors computed (not reused)
    seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(256))


class IndexChunkRow(Base):
    __tablename__ = "index_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_document_id: Mapped[int] = mapped_column(ForeignKey("index_documents.id"), index=True)
    document_id: Mapped[int] = mapped_column(Integer, index=True)
    chunk_key: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    embed_text: Mapped[str] = mapped_column(Text)
    embed_hash: Mapped[str] = mapped_column(String(40), index=True)
    parent_key: Mapped[str | None] = mapped_column(String(32))
    pages: Mapped[list] = mapped_column(JSON, default=list)
    section_path: Mapped[list] = mapped_column(JSON, default=list)
    speaker: Mapped[str | None] = mapped_column(String(32))
    table_caption: Mapped[str | None] = mapped_column(Text)
    study_ids: Mapped[list] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    guidance_section_id: Mapped[str | None] = mapped_column(String(64))
    # filters, copied from the document metadata (docs/metadata-strategy.md)
    program_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agency: Mapped[str | None] = mapped_column(String(16), index=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(8))
    doc_kind: Mapped[str | None] = mapped_column(String(16), index=True)
    doc_type: Mapped[str | None] = mapped_column(String(64))
    decision_date: Mapped[str | None] = mapped_column(String(10))
    guidance_status: Mapped[str | None] = mapped_column(String(32))
    guidance_effective_from: Mapped[str | None] = mapped_column(String(10))
    guidance_effective_to: Mapped[str | None] = mapped_column(String(10))
    embed_model: Mapped[str] = mapped_column(String(128), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    dense = mapped_column(Dense)
    sparse = mapped_column(Sparse)


class IndexParentRow(Base):
    __tablename__ = "index_parents"
    __table_args__ = (UniqueConstraint("index_document_id", "parent_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_document_id: Mapped[int] = mapped_column(ForeignKey("index_documents.id"), index=True)
    parent_key: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    pages: Mapped[list] = mapped_column(JSON, default=list)
