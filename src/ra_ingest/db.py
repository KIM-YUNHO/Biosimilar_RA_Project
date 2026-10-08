"""Relational store for the L0 product table, the L1 document index and run logs.
Works on SQLite (default, zero setup) and PostgreSQL (set --db postgresql+psycopg://...).
Upserts are written portably (select-then-insert/update) instead of dialect-specific SQL."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from .models import DocumentRecord, RegistrationRecord, TrialRecord
from .registry import Program


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ProgramRow(Base):
    __tablename__ = "programs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    ingredient: Mapped[str] = mapped_column(String(128), index=True)
    developer: Mapped[str | None] = mapped_column(String(256))
    codes: Mapped[list] = mapped_column(JSON, default=list)
    aliases: Mapped[dict] = mapped_column(JSON, default=dict)
    holdout: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegistrationRow(Base):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("agency", "native_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency: Mapped[str] = mapped_column(String(16), index=True)
    native_key: Mapped[str] = mapped_column(String(128))
    program_id: Mapped[str | None] = mapped_column(ForeignKey("programs.id"), index=True)
    brand_name: Mapped[str] = mapped_column(String(256))
    ingredient: Mapped[str | None] = mapped_column(String(256))
    identifiers: Mapped[dict] = mapped_column(JSON, default=dict)
    holder: Mapped[str | None] = mapped_column(String(512))
    licence_type: Mapped[str | None] = mapped_column(String(128))
    first_approval_date: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str | None] = mapped_column(String(128))
    indications: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128))
    source_snapshot: Mapped[str | None] = mapped_column(String(64))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("agency", "source_url"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency: Mapped[str] = mapped_column(String(16), index=True)
    doc_kind: Mapped[str] = mapped_column(String(16), index=True)
    doc_type: Mapped[str] = mapped_column(String(64), index=True)
    native_doc_type: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(1024))
    registration_id: Mapped[int | None] = mapped_column(ForeignKey("registrations.id"), index=True)
    program_id: Mapped[str | None] = mapped_column(ForeignKey("programs.id"), index=True)
    parent_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    procedure_id: Mapped[str | None] = mapped_column(String(128))
    decision_date: Mapped[str | None] = mapped_column(String(10))
    doc_date: Mapped[str | None] = mapped_column(String(10))
    source_published_at: Mapped[str | None] = mapped_column(String(10))
    source_updated_at: Mapped[str | None] = mapped_column(String(10))
    language: Mapped[str | None] = mapped_column(String(8))
    guidance: Mapped[dict | None] = mapped_column(JSON)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    fetch_status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|fetched|failed|skipped
    fetch_error: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    versions: Mapped[list["DocumentVersionRow"]] = relationship(back_populates="document")


class DocumentVersionRow(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "sha256"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    http_last_modified: Mapped[str | None] = mapped_column(String(64))
    etag: Mapped[str | None] = mapped_column(String(256))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document: Mapped[DocumentRow] = relationship(back_populates="versions")


class TrialRow(Base):
    __tablename__ = "trials"
    __table_args__ = (UniqueConstraint("nct_id", "program_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nct_id: Mapped[str] = mapped_column(String(32))
    program_id: Mapped[str | None] = mapped_column(ForeignKey("programs.id"))
    title: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(String(64))
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    primary_outcomes: Mapped[list] = mapped_column(JSON, default=list)
    sponsor: Mapped[str | None] = mapped_column(String(512))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestRunRow(Base):
    __tablename__ = "ingest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target: Mapped[dict] = mapped_column(JSON)
    agencies: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="running")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class IngestEventRow(Base):
    __tablename__ = "ingest_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ingest_runs.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(16))     # info|warning|error
    kind: Mapped[str] = mapped_column(String(64))      # source_failed|missing_registration|...
    agency: Mapped[str | None] = mapped_column(String(16))
    program_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Store:
    def __init__(self, url: str):
        self.engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # -- programs -----------------------------------------------------------
    def upsert_program(self, s: Session, p: Program) -> None:
        row = s.get(ProgramRow, p.id) or ProgramRow(id=p.id)
        row.role, row.ingredient, row.developer = p.role, p.ingredient, p.developer
        row.codes, row.aliases, row.holdout = p.codes, p.aliases, p.holdout
        row.updated_at = utcnow()
        s.add(row)

    # -- registrations -------------------------------------------------------
    def upsert_registration(self, s: Session, r: RegistrationRecord) -> RegistrationRow:
        row = s.scalar(select(RegistrationRow).where(
            RegistrationRow.agency == r.agency, RegistrationRow.native_key == r.native_key))
        if row is None:
            row = RegistrationRow(agency=r.agency, native_key=r.native_key)
            s.add(row)
        for f in ("program_id", "brand_name", "ingredient", "identifiers", "holder", "licence_type",
                  "first_approval_date", "status", "indications", "source", "source_snapshot", "raw"):
            new = getattr(r, f)
            if new not in (None, "", {}) or getattr(row, f, None) is None:
                setattr(row, f, new)
        row.last_seen_at = utcnow()
        s.flush()
        return row

    # -- documents -----------------------------------------------------------
    def upsert_document(self, s: Session, d: DocumentRecord, registration_id: int | None,
                        parent_id: int | None = None) -> DocumentRow:
        row = s.scalar(select(DocumentRow).where(
            DocumentRow.agency == d.agency, DocumentRow.source_url == d.source_url))
        if row is None:
            row = DocumentRow(agency=d.agency, source_url=d.source_url, fetch_status="pending")
            s.add(row)
        row.doc_kind, row.doc_type, row.native_doc_type = d.doc_kind, d.doc_type, d.native_doc_type
        row.title = d.title
        row.registration_id = registration_id or row.registration_id
        row.program_id = d.program_id or row.program_id
        row.parent_document_id = parent_id or row.parent_document_id
        for f in ("procedure_id", "decision_date", "doc_date", "source_published_at",
                  "source_updated_at", "language", "guidance"):
            new = getattr(d, f)
            if new is not None:
                setattr(row, f, new)
        row.extra = {**(row.extra or {}), **d.extra, "is_index_page": d.is_index_page}
        row.last_seen_at = utcnow()
        s.flush()
        return row

    def add_version(self, s: Session, doc: DocumentRow, sha: str, path: str, size: int,
                    content_type: str | None, last_modified: str | None, etag: str | None) -> bool:
        """Returns True when the bytes are new for this document (a new version)."""
        existing = s.scalar(select(DocumentVersionRow).where(
            DocumentVersionRow.document_id == doc.id, DocumentVersionRow.sha256 == sha))
        if existing:
            existing.last_verified_at = utcnow()
            return False
        s.add(DocumentVersionRow(document_id=doc.id, sha256=sha, storage_path=path, size_bytes=size,
                                 content_type=content_type, http_last_modified=last_modified, etag=etag))
        s.flush()
        return True

    # -- trials ----------------------------------------------------------------
    def upsert_trial(self, s: Session, t: TrialRecord) -> None:
        row = s.scalar(select(TrialRow).where(TrialRow.nct_id == t.nct_id,
                                              TrialRow.program_id == t.program_id))
        if row is None:
            row = TrialRow(nct_id=t.nct_id, program_id=t.program_id)
            s.add(row)
        row.title, row.phase, row.conditions = t.title, t.phase, t.conditions
        row.primary_outcomes, row.sponsor, row.raw = t.primary_outcomes, t.sponsor, t.raw
        row.retrieved_at = utcnow()

    # -- runs ------------------------------------------------------------------
    def start_run(self, target: dict[str, Any], agencies: list[str]) -> int:
        with self.session() as s:
            run = IngestRunRow(target=target, agencies=agencies)
            s.add(run)
            s.commit()
            return run.id

    def event(self, run_id: int, level: str, kind: str, message: str, agency: str | None = None,
              program_id: str | None = None, **detail: Any) -> None:
        with self.session() as s:
            s.add(IngestEventRow(run_id=run_id, level=level, kind=kind, agency=agency,
                                 program_id=program_id, message=message, detail=detail))
            s.commit()

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        with self.session() as s:
            run = s.get(IngestRunRow, run_id)
            run.status, run.stats, run.finished_at = status, stats, utcnow()
            s.commit()
