"""Preprocessing tables, created in the same database as ra-ingest (shared Base)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ra_ingest.db import Base, utcnow


class PageRouteRow(Base):
    __tablename__ = "page_routes"
    __table_args__ = (UniqueConstraint("sha256", "page"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    page: Mapped[int] = mapped_column(Integer)
    route: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str] = mapped_column(String(256))
    signals: Mapped[dict] = mapped_column(JSON, default=dict)


class PrepDocumentRow(Base):
    """One parse of one document version by one engine version."""
    __tablename__ = "prep_documents"
    __table_args__ = (UniqueConstraint("sha256", "engine"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    engine: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))          # ok | failed | skipped
    reason: Mapped[str | None] = mapped_column(Text)          # skip reason or error
    duplicate_of: Mapped[int | None] = mapped_column(Integer)
    output_path: Mapped[str | None] = mapped_column(String(1024))
    pages: Mapped[int | None] = mapped_column(Integer)
    seconds: Mapped[float | None] = mapped_column(Float)
    qa: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
