"""Command line entry point.

  ra-ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov
  ra-ingest ingest --product SB17 --product Wezenla --agencies ema
  ra-ingest ingest --ingredient adalimumab          any ingredient: programs come from the catalogs
  ra-ingest ingest --product Hadlima                any brand or FDA suffix name, no registry entry needed
  ra-ingest status
  ra-ingest manifest --out data/manifest.jsonl
  ra-ingest migrate-paths [--dry-run]          absolute file paths in old rows -> store keys
  ra-ingest rekey-html [--dry-run]             HTML versions: old fingerprint -> current
  ra-ingest copy-db --to postgresql+psycopg://ra:ra@localhost:5432/ra    SQLite -> PostgreSQL
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from .http import USER_AGENT, is_block_url
from .db import DocumentRow, IngestEventRow, IngestRunRow, ProgramRow, RegistrationRow, Store
from .metadata import build_document_metadata
from .models import AGENCIES
from .pipeline import ROOT, IngestConfig, run_ingest
from .settings import default_db, default_store

DEFAULT_DB = default_db()  # kept for scripts that import it


def _ingest(a: argparse.Namespace) -> int:
    cfg = IngestConfig(
        ingredient=a.ingredient, products=a.product or [],
        agencies=[x.strip() for x in a.agencies.split(",") if x.strip()],
        include_holdout=a.include_holdout, download=not a.no_download,
        auto_programs=not a.no_auto_programs, refill=not a.no_refill,
        doc_types=a.doc_types.split(",") if a.doc_types else None,
        db_url=a.db, store_url=a.data_dir or a.store,
    )
    try:
        result = run_ingest(cfg)
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    print(f"run {result.run_id}: {result.status}")
    print(json.dumps(result.stats, ensure_ascii=False, indent=2))
    _print_events(Store(a.db), result.run_id)
    return 0 if result.status == "ok" else 1


def _print_events(store: Store, run_id: int, limit: int = 40) -> None:
    with store.session() as s:
        rows = s.scalars(select(IngestEventRow).where(IngestEventRow.run_id == run_id)
                         .order_by(IngestEventRow.id)).all()
    if not rows:
        return
    print("\nevents:")
    for e in rows[:limit]:
        print(f"  [{e.level}] {e.agency or '-'} {e.kind}: {e.message}")
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more")


def _status(a: argparse.Namespace) -> int:
    store = Store(a.db)
    with store.session() as s:
        programs = s.scalars(select(ProgramRow).order_by(ProgramRow.role.desc(), ProgramRow.id)).all()
        regs = s.scalars(select(RegistrationRow)).all()
        docs = s.scalars(select(DocumentRow)).all()
        last_run = s.scalar(select(IngestRunRow).order_by(IngestRunRow.id.desc()))
    by_prog: dict[tuple[str, str], list[RegistrationRow]] = defaultdict(list)
    for r in regs:
        by_prog[(r.program_id or "-", r.agency)].append(r)
    doc_count: Counter = Counter()
    for d in docs:
        if d.doc_kind == "product" and d.fetch_status != "superseded":
            doc_count[(d.program_id or "-", d.agency, d.fetch_status)] += 1
    print(f"{'program':<10} " + " ".join(f"{ag.upper():<34}" for ag in AGENCIES))
    for p in programs:
        cells = []
        for ag in AGENCIES:
            rs = by_prog.get((p.id, ag), [])
            primary = ((p.aliases or {}).get(ag) or [""])[0].lower()
            rs = sorted(rs, key=lambda r: (r.brand_name.lower() != primary, r.native_key))
            extra = f"+{len(rs) - 1}" if len(rs) > 1 else ""
            name = (rs[0].brand_name + extra) if rs else "—"
            date = rs[0].first_approval_date if rs and rs[0].first_approval_date else ""
            n_ok = doc_count[(p.id, ag, "fetched")]
            n_all = sum(v for (pid, a2, _), v in doc_count.items() if pid == p.id and a2 == ag)
            cells.append(f"{name[:14]:<14} {date:<10} docs {n_ok}/{n_all}".ljust(34))
        flag = " (holdout)" if p.holdout else ""
        print(f"{p.id:<10} " + " ".join(cells) + flag)
    unmapped = [r for r in regs if r.program_id is None]
    if unmapped:
        print(f"\n레지스트리 밖 같은 성분 제품 {len(unmapped)}건: " +
              ", ".join(sorted({f'{r.brand_name}({r.agency})' for r in unmapped})))
    g = [d for d in docs if d.doc_kind == "guidance"]
    if g:
        c = Counter(d.fetch_status for d in g)
        print(f"\n가이던스 {len(g)}건: " + ", ".join(f"{k} {v}" for k, v in c.items()))
    if last_run:
        print(f"\n마지막 실행 #{last_run.id}: {last_run.status} ({last_run.started_at:%Y-%m-%d %H:%M} UTC)")
    return 0


def _manifest(a: argparse.Namespace) -> int:
    """Document manifest with the full document-level metadata contract (JSON Lines).
    The preprocessing stage reads this instead of querying the database directly."""
    store = Store(a.db)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with store.session() as s, out.open("w", encoding="utf-8") as f:
        q = select(DocumentRow)
        if not a.all:
            q = q.where(DocumentRow.fetch_status == "fetched")
        for doc in s.scalars(q.order_by(DocumentRow.id)):
            f.write(json.dumps(build_document_metadata(s, doc), ensure_ascii=False, default=str) + "\n")
            n += 1
    print(f"{n} documents -> {out}")
    return 0


CHECK_HOSTS = {
    "ema": ["https://www.ema.europa.eu/"],
    "fda": ["https://www.accessdata.fda.gov/", "https://www.fda.gov/"],
    "hc": ["https://health-products.canada.ca/", "https://dhpp.hpfb-dgpsa.ca/", "https://www.canada.ca/",
           "https://pdf.hres.ca/"],
    "ctgov": ["https://clinicaltrials.gov/"],
}


def _check(a: argparse.Namespace) -> int:
    """Quick reachability check per agency host (one request each, no retries)."""
    import requests
    ok_all = True
    for agency, urls in CHECK_HOSTS.items():
        for url in urls:
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
                code = r.status_code
                msg = f"HTTP {code}"
                ok = code < 500 and code != 403
                if is_block_url(r.url):
                    msg, ok = f"HTTP {code} 봇 차단 페이지로 이동(이 호스트 문서는 받을 수 없음)", False
            except requests.RequestException as e:
                msg, ok = type(e).__name__ + (": 403 (네트워크 정책 차단)" if "403" in str(e) else ""), False
            ok_all &= ok
            print(f"{'OK ' if ok else 'NG '} {agency:<6} {url:<42} {msg}")
    return 0 if ok_all else 1


def _migrate_paths(a: argparse.Namespace) -> int:
    from .migrate import migrate_paths
    from .objstore import open_store

    stats = migrate_paths(a.db, open_store(a.store), dry_run=a.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _rekey_html(a: argparse.Namespace) -> int:
    from .migrate import rekey_html_versions
    from .objstore import open_store

    stats = rekey_html_versions(a.db, open_store(a.store), dry_run=a.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _copy_db(a: argparse.Namespace) -> int:
    from .migrate import copy_database
    from .objstore import open_store

    print(f"copy {a.db} -> {a.to}")
    counts = copy_database(a.db, a.to, open_store(a.store))
    print(f"done: {sum(counts.values())} rows in {len(counts)} tables")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ra-ingest", description="바이오시밀러 RA 문서 적재")
    ap.add_argument("--db", default=default_db(),
                    help="SQLAlchemy URL (기본: 환경변수 RA_DB, 없으면 SQLite data/ra.db)")
    ap.add_argument("--store", default=default_store(),
                    help="원문 저장소: 디렉터리 또는 s3://bucket/prefix (기본: RA_STORE, 없으면 data/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="성분 또는 제품을 지정해 기관별로 적재")
    i.add_argument("--ingredient", help="성분명, 예: ustekinumab")
    i.add_argument("--product", action="append", help="개발 코드/브랜드명/FDA 접미사 이름 (반복 가능)")
    i.add_argument("--agencies", default="ema,fda,hc,guidance,ctgov",
                   help="쉼표 구분: ema,fda,hc,guidance,ctgov")
    i.add_argument("--include-holdout", action="store_true", help="holdout 제품(BAT2206)도 포함")
    i.add_argument("--no-download", action="store_true", help="메타데이터만 수집")
    i.add_argument("--doc-types", help="다운로드할 문서 유형 제한(쉼표 구분)")
    i.add_argument("--no-auto-programs", action="store_true",
                   help="레지스트리(config/products.yaml)에 없는 제품은 문서를 받지 않음(이전 동작)")
    i.add_argument("--no-refill", action="store_true",
                   help="카탈로그에 없던 기존 문서를 저장된 URL로 다시 받지 않음")
    i.add_argument("--data-dir", help="(이전 옵션) --store와 같음")
    i.set_defaults(func=_ingest)

    c = sub.add_parser("check", help="기관 사이트 접속 점검")
    c.set_defaults(func=_check)

    st = sub.add_parser("status", help="제품 × 기관 적재 현황")
    st.set_defaults(func=_status)

    m = sub.add_parser("manifest", help="문서 메타데이터 매니페스트(JSONL) 내보내기")
    m.add_argument("--out", default=str(ROOT / "data" / "manifest.jsonl"))
    m.add_argument("--all", action="store_true", help="수집 실패/대기 문서도 포함")
    m.set_defaults(func=_manifest)

    mp = sub.add_parser("migrate-paths", help="이전 행의 절대 경로를 저장소 키로 변환")
    mp.add_argument("--dry-run", action="store_true")
    mp.set_defaults(func=_migrate_paths)

    rk = sub.add_parser("rekey-html", help="HTML 문서 버전의 sha256을 현재 지문 방식으로 옮김(조회일 때문에 매일 새 버전이 생기던 문제)")
    rk.add_argument("--dry-run", action="store_true")
    rk.set_defaults(func=_rekey_html)

    cp = sub.add_parser("copy-db", help="--db의 모든 표를 빈 DB(--to)로 복사 (예: SQLite -> PostgreSQL)")
    cp.add_argument("--to", required=True, help="대상 SQLAlchemy URL")
    cp.set_defaults(func=_copy_db)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
