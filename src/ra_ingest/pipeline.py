"""Ingestion orchestration: target -> per-agency registrations -> programs -> documents -> raw versions.

Usage from code (the later RAG/agent framework calls this the same way the CLI does):

    from ra_ingest.pipeline import IngestConfig, run_ingest
    run_ingest(IngestConfig(ingredient="adalimumab"))           # any ingredient
    run_ingest(IngestConfig(products=["Wezlana"]))              # any product (brand, FDA suffix name)

Neither has to be in config/products.yaml. What the registry does not list is derived from
the agency catalogs (autoprogram.py) and recorded as programs, so a request is enough.

Stages of one run
  1. catalogs      every agency's registrations for the target (L0)
  2. programs      registrations -> registry programs (curated) -> derived programs (auto)
  3. documents     per registration: discover, download, version (L1)
  4. refill        documents the database knows that this run's catalogs did not list
                   (catalog down, entry removed) are fetched by their stored URL
  5. guidance, ClinicalTrials.gov
A failure of one source, one document or one host never stops the others; it is recorded
(`ingest_events`, `documents.fetch_error`) and the run ends `partial`.
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .autoprogram import base_ingredient, build_programs
from .connectors.base import Connector
from .connectors.ctgov import trials_for_program
from .connectors.ema import EmaConnector
from .connectors.fda import FdaConnector
from .connectors.guidance import guidance_documents, load_guidances
from .connectors.hc import HcConnector
from .db import DocumentRow, DocumentVersionRow, Store
from .http import FetchError, Fetcher, FetchResult
from .models import AGENCIES, DocumentRecord, RegistrationRecord
from .objstore import LocalStore, open_store
from .registry import Registry, Target, validate_agencies
from .settings import default_db
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
    auto_programs: bool = True               # derive programs for catalog products the registry lacks
    refill: bool = True                      # fetch known documents the catalogs did not list
    db_url: str = field(default_factory=default_db)
    data_dir: Path = ROOT / "data"           # local store directory, used when store_url is None
    store_url: str | None = None             # object store (directory or s3://...), see objstore
    registry_path: Path = ROOT / "config" / "products.yaml"
    guidances_path: Path = ROOT / "config" / "guidances.yaml"


@dataclass
class IngestResult:
    run_id: int
    status: str
    stats: dict[str, Any]


class _Run:
    def __init__(self, cfg: IngestConfig, store: Store, fetcher: Fetcher,
                 connectors: dict[str, Connector] | None, registry_ids: set[str] | None = None):
        self.cfg, self.store, self.fetcher = cfg, store, fetcher
        self.fetcher.clear_cache()
        self.raw = RawStore(open_store(cfg.store_url) if cfg.store_url else LocalStore(cfg.data_dir))
        self.stats: dict[str, Any] = {"registrations": 0, "unmapped_registrations": 0, "auto_programs": 0,
                                      "documents": 0, "fetched": 0, "new_versions": 0, "fetch_failed": 0,
                                      "skipped": 0, "refilled": 0, "trials": 0, "errors": 0, "warnings": 0}
        self.injected = connectors or {}
        self.registry_ids = registry_ids or set()
        self.conns: dict[str, Connector] = {}
        self.handled: set[int] = set()       # document rows seen through a catalog in this run

    def report_for(self, agency: str | None):
        def report(level: str, kind: str, message: str, **detail: Any) -> None:
            if level in ("error", "warning"):
                self.stats[level + "s"] += 1
            program_id = detail.pop("program_id", None)
            ag = detail.pop("agency", agency)
            self.store.event(self.run_id, level, kind, message, agency=ag, program_id=program_id, **detail)
        return report

    def connector(self, agency: str) -> Connector:
        if agency not in self.conns:
            self.conns[agency] = (self.injected[agency] if agency in self.injected
                                  else CONNECTORS[agency](self.fetcher, self.report_for(agency)))
        return self.conns[agency]

    # ---------------------------------------------------------------------------
    def execute(self, target: Target) -> IngestResult:
        cfg = self.cfg
        self.run_id = self.store.start_run(
            {"ingredient": target.ingredient, "products": cfg.products,
             "programs": [p.id for p in target.programs], "include_holdout": cfg.include_holdout,
             "auto": target.auto and cfg.auto_programs},
            cfg.agencies)
        try:
            self._execute(target)
        except Exception as e:  # never leave the run row at "running"
            self.report_for(None)("error", "run_crashed", f"{type(e).__name__}: {e}")
            self.store.finish_run(self.run_id, "failed", dict(self.stats))
            raise
        if self.stats["errors"] == 0 and self.stats["fetch_failed"] == 0:
            status = "ok"
        elif self.stats["registrations"] or self.stats["fetched"]:
            status = "partial"
        else:
            status = "failed"
        self.store.finish_run(self.run_id, status, self.stats)
        return IngestResult(self.run_id, status, dict(self.stats))

    def _execute(self, target: Target) -> None:
        cfg = self.cfg
        agencies = [a for a in cfg.agencies if a in AGENCIES]
        with self.store.session() as s:
            for p in target.programs:
                self.store.upsert_program(s, p)
            s.commit()

        catalog = self._collect(target, agencies)
        target, catalog, skip = self._map_programs(target, catalog, agencies)
        for agency in agencies:
            self._ingest_agency(agency, target, catalog.get(agency, []), skip)
        if cfg.refill and cfg.download:
            self._refill(target, agencies)
        if "guidance" in cfg.agencies:
            self._ingest_guidance([a for a in cfg.agencies if a in AGENCIES] or list(AGENCIES))
        if "ctgov" in cfg.agencies:
            self._ingest_trials(target)
        for host in self.fetcher.blocked_hosts():
            self.report_for(None)("warning", "host_blocked",
                                  f"{host}: 봇 차단으로 요청을 건너뜀(이 호스트의 문서는 나중에 다시 실행하면 채워짐)")

    # -- 1. catalogs ------------------------------------------------------------
    def _collect(self, target: Target, agencies: list[str]) -> dict[str, list[RegistrationRecord]]:
        out: dict[str, list[RegistrationRecord]] = {}
        for agency in agencies:
            conn = self.connector(agency)
            if isinstance(conn, HcConnector):
                conn.set_seeds({p.id: p.seed_documents.get("hc", []) for p in target.programs})
            try:
                out[agency] = list(conn.discover_registrations(target))
            except FetchError as e:
                kind = "source_blocked" if e.blocked else "source_failed"
                self.report_for(agency)("error", kind, f"{agency} 카탈로그 수집 실패: {e.reason}", url=e.url)
                out[agency] = []
            except Exception as e:
                self.report_for(agency)("error", "source_failed", f"{agency} 카탈로그 처리 오류: {type(e).__name__}: {e}")
                out[agency] = []
        return out

    # -- 2. programs ------------------------------------------------------------
    def _map_programs(self, target: Target, catalog: dict[str, list[RegistrationRecord]],
                      agencies: list[str]) -> tuple[Target, dict[str, list[RegistrationRecord]], set[int]]:
        """Set `program_id` on every registration. Returns the (extended) target, the catalog
        to ingest and the ids of registrations to leave out of this run entirely."""
        report = self.report_for(None)
        auto = self.cfg.auto_programs
        seeds: set[tuple[str, str]] = set()
        wanted_only = False

        if target.requested and auto:
            # product names the registry does not know: what matched them identifies the
            # ingredient; the ingredient-level catalog then shows the rest of the programme
            # (the EU brand differs from the US one) so the products can be clustered.
            for agency, regs in catalog.items():
                for reg in regs:
                    if target.match_program(agency, reg) is None:
                        seeds.add((agency, reg.native_key))
            if not seeds:
                report("error", "no_match", f"요청한 제품을 어느 기관 카탈로그에서도 찾지 못함: {', '.join(target.requested)}")
                return target, {a: [] for a in agencies}, set()
            ingredients = Counter(base_ingredient(r) for regs in catalog.values() for r in regs
                                  if (r.agency, r.native_key) in seeds and base_ingredient(r))
            ingredient = target.ingredient or (ingredients.most_common(1)[0][0] if ingredients else None)
            if ingredient:
                wide = dataclasses.replace(target, ingredient=ingredient, product_scoped=False,
                                           ingredient_synonyms=[ingredient, f"{ingredient}-"], requested=[])
                catalog = self._collect(wide, agencies)
                target, wanted_only = wide, True

        for agency, regs in catalog.items():
            for reg in regs:
                reg.program_id = target.match_program(agency, reg)

        skip: set[int] = set()
        unmapped = [(a, r) for a, regs in catalog.items() for r in regs if r.program_id is None]
        held_out = {id(r) for _, r in unmapped if target.is_excluded(r.agency, r)}
        if auto:
            todo = [r for _, r in unmapped if id(r) not in held_out]
            if todo:
                result = build_programs(todo, taken=self.registry_ids | {p.id for p in target.programs},
                                        has_reference=any(p.role == "reference" for p in target.programs))
                keep = {pid for (a, k), pid in result.assignment.items() if (a, k) in seeds} if wanted_only else None
                new = [p for p in result.programs if keep is None or p.id in keep]
                for p in new:
                    target.programs.append(p)
                    with self.store.session() as s:
                        self.store.upsert_program(s, p)
                        s.commit()
                    members = {a: sorted({r.brand_name for r in todo if result.assignment.get((r.agency, r.native_key)) == p.id
                                          and r.agency == a}) for a in AGENCIES}
                    desc = "; ".join(f"{a}={'+'.join(v)}" for a, v in members.items() if v)
                    report("info", "auto_program",
                           f"{p.id} ({p.role}, {p.developer or '?'}): {desc} [기준: {', '.join(result.basis[p.id])}]",
                           program_id=p.id)
                self.stats["auto_programs"] += len(new)
                for _, r in unmapped:
                    pid = result.assignment.get((r.agency, r.native_key))
                    if pid is None:
                        continue
                    if keep is None or pid in keep:
                        r.program_id = pid
                    else:
                        skip.add(id(r))     # a programme of the ingredient nobody asked for
        if wanted_only:
            wanted = {p.id for p in target.programs}
            for regs in catalog.values():
                for r in regs:
                    if r.program_id not in wanted:
                        skip.add(id(r))
        return target, catalog, skip

    # -- 3. documents -----------------------------------------------------------
    def _ingest_agency(self, agency: str, target: Target, regs: list[RegistrationRecord], skip: set[int]) -> None:
        report = self.report_for(agency)
        conn = self.connector(agency)
        found_programs: set[str] = set()
        for reg in regs:
            if id(reg) in skip:
                continue
            with self.store.session() as s:
                row = self.store.upsert_registration(s, reg)
                s.commit()
                reg_id = row.id
            self.stats["registrations"] += 1
            if reg.program_id is None:
                self.stats["unmapped_registrations"] += 1
                report("info", "unmapped_registration",
                       f"레지스트리에 없는 같은 성분 제품: {reg.brand_name} ({reg.native_key})")
                continue  # catalog row kept; documents only for programs
            found_programs.add(reg.program_id)
            try:
                for doc in conn.discover_documents(reg, target):
                    self._handle_document(conn, doc, reg_id)
            except FetchError as e:
                report("error", "source_blocked" if e.blocked else "source_failed",
                       f"{reg.brand_name} 문서 목록 수집 실패: {e.reason}", url=e.url, program_id=reg.program_id)
            except Exception as e:
                report("error", "source_failed",
                       f"{reg.brand_name} 문서 목록 처리 오류: {type(e).__name__}: {e}", program_id=reg.program_id)

        derived = {p.id for p in target.programs} - self.registry_ids
        for p in target.programs:
            if p.id in found_programs:
                continue
            if p.id in derived:
                report("info", "not_registered", f"{p.id}: {agency} 카탈로그에는 없음", program_id=p.id)
            elif p.expected_at(agency):
                report("warning", "missing_registration", f"{p.id}: {agency} 카탈로그에서 찾지 못함",
                       program_id=p.id)
            else:
                report("info", "expected_absent",
                       f"{p.id}: {agency} 승인 없음으로 등록됨 — {p.agency_status[agency].get('note', '')}",
                       program_id=p.id)

    def _fail(self, doc_id: int, reason: str) -> None:
        """A fetch failed. The bytes of an earlier successful fetch stay valid, so a document
        that has a version keeps its status and only records the latest error."""
        with self.store.session() as s:
            r = s.get(DocumentRow, doc_id)
            has_version = s.scalar(select(func.count()).select_from(DocumentVersionRow)
                                   .where(DocumentVersionRow.document_id == doc_id)) > 0
            r.fetch_error = reason
            if not has_version or r.fetch_status == "pending":
                r.fetch_status = "failed"
            s.commit()
        self.stats["fetch_failed"] += 1

    def _download(self, doc_id: int, url: str, agency: str) -> FetchResult | None:
        """GET, store the bytes, record the version. None when it failed (recorded)."""
        try:
            res = self.fetcher.get(url)
        except FetchError as e:
            self._fail(doc_id, e.reason)
            return None
        expects_pdf = url.lower().endswith(".pdf") or "/download" in url
        if expects_pdf and not res.content.startswith(b"%PDF") and b"<html" in res.content[:2000].lower():
            # WAF / rate-limit pages ("Sorry", "apology") come back as 200 HTML
            self._fail(doc_id, "HTML page returned instead of PDF (blocked or moved)")
            return None
        sha, key = self.raw.put(agency, res.content, guess_ext(res.url, res.content_type))
        with self.store.session() as s:
            r = s.get(DocumentRow, doc_id)
            if self.store.add_version(s, r, sha, key, len(res.content), res.content_type,
                                      res.last_modified, res.etag):
                self.stats["new_versions"] += 1
            r.fetch_status, r.fetch_error = "fetched", None
            s.commit()
        return res

    def _handle_document(self, conn: Connector | None, doc: DocumentRecord, reg_id: int | None,
                         parent_id: int | None = None) -> None:
        with self.store.session() as s:
            row = self.store.upsert_document(s, doc, reg_id, parent_id)
            s.commit()
            row_id = row.id
        self.handled.add(row_id)
        self.stats["documents"] += 1
        wanted = self.cfg.download and (self.cfg.doc_types is None or doc.doc_type in self.cfg.doc_types
                                        or doc.is_index_page)
        if not wanted:
            with self.store.session() as s:      # a document fetched earlier stays fetched
                r = s.get(DocumentRow, row_id)
                has_version = s.scalar(select(func.count()).select_from(DocumentVersionRow)
                                       .where(DocumentVersionRow.document_id == row_id)) > 0
                if not has_version:
                    r.fetch_status, r.fetch_error = "skipped", None
                s.commit()
            self.stats["skipped"] += 1
            return
        try:
            res = self._download(row_id, doc.source_url, doc.agency)
            if res is None:
                return
            self.stats["fetched"] += 1
            if conn is not None:
                self._apply_after_fetch(conn.after_fetch(doc, res), row_id, reg_id)
            if doc.is_index_page and self.cfg.expand_index_pages and conn is not None:
                for child in conn.expand_index(doc, res):
                    self._handle_document(conn, child, reg_id, parent_id=row_id)
        except Exception as e:  # one broken document must not stop the run
            self._fail(row_id, f"{type(e).__name__}: {e}")
            self.report_for(doc.agency)("error", "document_error", f"{doc.source_url}: {type(e).__name__}: {e}")

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

    # -- 4. refill ----------------------------------------------------------------
    def _refill(self, target: Target, agencies: list[str]) -> None:
        """Known documents of the target that no catalog listed in this run and whose newest
        version is not in the object store: fetch them by the stored URL. This is what keeps
        the store complete when a catalog is down, blocked, or dropped an entry."""
        program_ids = {p.id for p in target.programs}
        with self.store.session() as s:
            q = select(DocumentRow).where(DocumentRow.agency.in_(agencies),
                                          DocumentRow.fetch_status != "superseded")
            rows = [d for d in s.scalars(q) if d.id not in self.handled
                    and ((d.doc_kind == "product" and d.program_id in program_ids)
                         or (d.doc_kind == "guidance" and "guidance" in self.cfg.agencies))
                    and (self.cfg.doc_types is None or d.doc_type in self.cfg.doc_types)]
            todo = []
            for d in rows:
                newest = s.scalar(select(DocumentVersionRow).where(DocumentVersionRow.document_id == d.id)
                                  .order_by(DocumentVersionRow.retrieved_at.desc(), DocumentVersionRow.id.desc()))
                if newest is not None and self.raw.store.exists(newest.storage_path):
                    continue
                todo.append((d.id, d.source_url, d.agency))
        for doc_id, url, agency in todo:
            if self._download(doc_id, url, agency) is not None:
                self.stats["refilled"] += 1

    # -- 5. guidance, trials ------------------------------------------------------
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
    if cfg.store_url is None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_url)
    return _Run(cfg, store, fetcher or Fetcher(), connectors,
                {p.id for p in registry.programs}).execute(target)
