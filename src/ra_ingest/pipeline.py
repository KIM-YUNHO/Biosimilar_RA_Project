"""Ingestion orchestration: target -> per-agency registrations -> documents -> raw versions.

Usage from code (the later RAG/agent framework calls this the same way the CLI does):

    from ra_ingest.pipeline import IngestConfig, run_ingest
    result = run_ingest(IngestConfig(ingredient="ustekinumab", agencies=["ema", "fda", "hc"]))
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connectors.base import Connector
from .connectors.ctgov import trials_for_program
from .connectors.ema import EmaConnector
from .connectors.fda import FdaConnector
from .connectors.guidance import guidance_documents, load_guidances
from .connectors.hc import HcConnector
from .db import DocumentRow, Store
from .http import FetchError, Fetcher
from .models import AGENCIES, DocumentRecord
from .registry import Registry, Target, validate_agencies
from .storage import RawStore, guess_ext

ROOT = Path(__file__).resolve().parents[2]

CONNECTORS: dict[str, type[Connector]] = {"ema": EmaConnector, "fda": FdaConnector, "hc": HcConnector}


@dataclass
class IngestConfig:
    ingredient: str | None = None
    products: list[str] = field(default_factory=list)
    agencies: list[str] = field(default_factory=lambda: ["ema", "fda", "hc", "guidance", "ctgov"])
    include_holdout: bool = False
    download: bool = True
    doc_types: list[str] | None = None       # restrict downloads, e.g. ["assessment_report", "review"]
    expand_index_pages: bool = True
    db_url: str = f"sqlite:///{ROOT / 'data' / 'ra.db'}"
    data_dir: Path = ROOT / "data"
    registry_path: Path = ROOT / "config" / "products.yaml"
    guidances_path: Path = ROOT / "config" / "guidances.yaml"


@dataclass
class IngestResult:
    run_id: int
    status: str
    stats: dict[str, Any]


class _Run:
    def __init__(self, cfg: IngestConfig, store: Store, fetcher: Fetcher, connectors: dict[str, Connector] | None):
        self.cfg, self.store, self.fetcher = cfg, store, fetcher
        self.raw = RawStore(cfg.data_dir)
        self.stats: dict[str, Any] = {"registrations": 0, "unmapped_registrations": 0, "documents": 0,
                                      "fetched": 0, "new_versions": 0, "fetch_failed": 0, "skipped": 0,
                                      "trials": 0, "errors": 0, "warnings": 0}
        self.injected = connectors or {}

    def report_for(self, agency: str | None):
        def report(level: str, kind: str, message: str, **detail: Any) -> None:
            if level in ("error", "warning"):
                self.stats[level + "s"] += 1
            program_id = detail.pop("program_id", None)
            ag = detail.pop("agency", agency)
            self.store.event(self.run_id, level, kind, message, agency=ag, program_id=program_id, **detail)
        return report

    def connector(self, agency: str) -> Connector:
        if agency in self.injected:
            return self.injected[agency]
        return CONNECTORS[agency](self.fetcher, self.report_for(agency))

    # ---------------------------------------------------------------------------
    def execute(self, target: Target) -> IngestResult:
        cfg = self.cfg
        self.run_id = self.store.start_run(
            {"ingredient": target.ingredient, "products": cfg.products,
             "programs": [p.id for p in target.programs], "include_holdout": cfg.include_holdout},
            cfg.agencies)
        with self.store.session() as s:
            for p in target.programs:
                self.store.upsert_program(s, p)
            s.commit()

        for agency in [a for a in cfg.agencies if a in AGENCIES]:
            self._ingest_agency(agency, target)
        if "guidance" in cfg.agencies:
            self._ingest_guidance([a for a in cfg.agencies if a in AGENCIES] or list(AGENCIES))
        if "ctgov" in cfg.agencies:
            self._ingest_trials(target)

        if self.stats["errors"] == 0 and self.stats["fetch_failed"] == 0:
            status = "ok"
        elif self.stats["registrations"] or self.stats["fetched"]:
            status = "partial"
        else:
            status = "failed"
        self.store.finish_run(self.run_id, status, self.stats)
        return IngestResult(self.run_id, status, dict(self.stats))

    def _ingest_agency(self, agency: str, target: Target) -> None:
        report = self.report_for(agency)
        conn = self.connector(agency)
        if isinstance(conn, HcConnector):
            conn.set_seeds({p.id: p.seed_documents.get("hc", []) for p in target.programs})
        try:
            regs = list(conn.discover_registrations(target))
        except FetchError as e:
            report("error", "source_failed", f"{agency} 카탈로그 수집 실패: {e.reason}", url=e.url)
            regs = []
        found_programs: set[str] = set()
        for reg in regs:
            reg.program_id = target.match_program(agency, reg)
            with self.store.session() as s:
                row = self.store.upsert_registration(s, reg)
                s.commit()
                reg_id = row.id
            self.stats["registrations"] += 1
            if reg.program_id is None:
                self.stats["unmapped_registrations"] += 1
                report("info", "unmapped_registration",
                       f"레지스트리에 없는 같은 성분 제품: {reg.brand_name} ({reg.native_key})")
                continue  # catalog row kept; documents only for registry programs
            found_programs.add(reg.program_id)
            for doc in conn.discover_documents(reg, target):
                self._handle_document(conn, doc, reg_id)

        for p in target.programs:
            if p.id in found_programs:
                continue
            if p.expected_at(agency):
                report("warning", "missing_registration", f"{p.id}: {agency} 카탈로그에서 찾지 못함",
                       program_id=p.id)
            else:
                report("info", "expected_absent",
                       f"{p.id}: {agency} 승인 없음으로 등록됨 — {p.agency_status[agency].get('note', '')}",
                       program_id=p.id)

    def _handle_document(self, conn: Connector | None, doc: DocumentRecord, reg_id: int | None,
                         parent_id: int | None = None) -> None:
        with self.store.session() as s:
            row = self.store.upsert_document(s, doc, reg_id, parent_id)
            s.commit()
        self.stats["documents"] += 1
        wanted = self.cfg.download and (self.cfg.doc_types is None or doc.doc_type in self.cfg.doc_types
                                        or doc.is_index_page)
        if not wanted:
            self._set_fetch(row.id, "skipped", None)
            self.stats["skipped"] += 1
            return
        try:
            res = self.fetcher.get(doc.source_url)
        except FetchError as e:
            self._set_fetch(row.id, "failed", e.reason)
            self.stats["fetch_failed"] += 1
            return
        expects_pdf = doc.source_url.lower().endswith(".pdf") or "/download" in doc.source_url
        if expects_pdf and not res.content.startswith(b"%PDF") and b"<html" in res.content[:2000].lower():
            # WAF / rate-limit pages ("Sorry", "apology") come back as 200 HTML
            self._set_fetch(row.id, "failed", "HTML page returned instead of PDF (blocked or moved)")
            self.stats["fetch_failed"] += 1
            return
        sha, path = self.raw.put(doc.agency, res.content, guess_ext(res.url, res.content_type))
        with self.store.session() as s:
            r = s.get(DocumentRow, row.id)
            if self.store.add_version(s, r, sha, str(path), len(res.content), res.content_type,
                                      res.last_modified, res.etag):
                self.stats["new_versions"] += 1
            r.fetch_status, r.fetch_error = "fetched", None
            s.commit()
        self.stats["fetched"] += 1
        if conn is not None:
            self._apply_after_fetch(conn.after_fetch(doc, res), row.id, reg_id)
        if doc.is_index_page and self.cfg.expand_index_pages and conn is not None:
            for child in conn.expand_index(doc, res):
                self._handle_document(conn, child, reg_id, parent_id=row.id)

    def _apply_after_fetch(self, updates: dict, doc_id: int, reg_id: int | None) -> None:
        if not updates:
            return
        from .db import RegistrationRow
        with self.store.session() as s:
            d = s.get(DocumentRow, doc_id)
            for k, v in (updates.get("document") or {}).items():
                if v:
                    setattr(d, k, v)
            if reg_id and updates.get("registration"):
                r = s.get(RegistrationRow, reg_id)
                for k, v in updates["registration"].items():
                    if not v:
                        continue
                    if k.startswith("identifiers."):
                        r.identifiers = {**(r.identifiers or {}), k.split(".", 1)[1]: v}
                    elif k == "first_approval_date" and r.first_approval_date and r.first_approval_date <= v:
                        continue  # keep the earliest
                    else:
                        setattr(r, k, v)
            s.commit()

    def _set_fetch(self, doc_id: int, status: str, error: str | None) -> None:
        with self.store.session() as s:
            r = s.get(DocumentRow, doc_id)
            r.fetch_status, r.fetch_error = status, error
            s.commit()

    def _ingest_guidance(self, agencies: list[str]) -> None:
        report = self.report_for(None)
        for doc in guidance_documents(load_guidances(self.cfg.guidances_path), agencies, report, self.fetcher):
            self._handle_document(None, doc, None)

    def _ingest_trials(self, target: Target) -> None:
        report = self.report_for("ctgov")
        for p in target.programs:
            if p.role != "biosimilar":
                continue
            with self.store.session() as s:
                for t in trials_for_program(self.fetcher, p, report):
                    self.store.upsert_trial(s, t)
                    self.stats["trials"] += 1
                s.commit()


def run_ingest(cfg: IngestConfig, fetcher: Fetcher | None = None,
               connectors: dict[str, Connector] | None = None) -> IngestResult:
    validate_agencies(cfg.agencies)
    registry = Registry.load(cfg.registry_path)
    target = registry.resolve(cfg.ingredient, cfg.products or None, cfg.include_holdout)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_url)
    return _Run(cfg, store, fetcher or Fetcher(), connectors).execute(target)
