"""Command line entry point.

  ra-ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov
  ra-ingest ingest --product SB17 --product Wezenla --agencies ema
  ra-ingest status
  ra-ingest manifest --out data/manifest.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from .db import DocumentRow, IngestEventRow, IngestRunRow, ProgramRow, RegistrationRow, Store
from .metadata import build_document_metadata
from .models import AGENCIES
from .pipeline import ROOT, IngestConfig, run_ingest

DEFAULT_DB = f"sqlite:///{ROOT / 'data' / 'ra.db'}"


def _ingest(a: argparse.Namespace) -> int:
    cfg = IngestConfig(
        ingredient=a.ingredient, products=a.product or [],
        agencies=[x.strip() for x in a.agencies.split(",") if x.strip()],
        include_holdout=a.include_holdout, download=not a.no_download,
        doc_types=a.doc_types.split(",") if a.doc_types else None,
        db_url=a.db, data_dir=Path(a.data_dir),
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
                code = requests.get(url, timeout=15).status_code
                msg = f"HTTP {code}"
                ok = code < 500 and code != 403
            except requests.RequestException as e:
                msg, ok = type(e).__name__ + (": 403 (네트워크 정책 차단)" if "403" in str(e) else ""), False
            ok_all &= ok
            print(f"{'OK ' if ok else 'NG '} {agency:<6} {url:<42} {msg}")
    return 0 if ok_all else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ra-ingest", description="바이오시밀러 RA 문서 적재")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLAlchemy URL (기본: SQLite data/ra.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="성분 또는 제품을 지정해 기관별로 적재")
    i.add_argument("--ingredient", help="성분명, 예: ustekinumab")
    i.add_argument("--product", action="append", help="개발 코드/브랜드명/FDA 접미사 이름 (반복 가능)")
    i.add_argument("--agencies", default="ema,fda,hc,guidance,ctgov",
                   help="쉼표 구분: ema,fda,hc,guidance,ctgov")
    i.add_argument("--include-holdout", action="store_true", help="holdout 제품(BAT2206)도 포함")
    i.add_argument("--no-download", action="store_true", help="메타데이터만 수집")
    i.add_argument("--doc-types", help="다운로드할 문서 유형 제한(쉼표 구분)")
    i.add_argument("--data-dir", default=str(ROOT / "data"))
    i.set_defaults(func=_ingest)

    c = sub.add_parser("check", help="기관 사이트 접속 점검")
    c.set_defaults(func=_check)

    st = sub.add_parser("status", help="제품 × 기관 적재 현황")
    st.set_defaults(func=_status)

    m = sub.add_parser("manifest", help="문서 메타데이터 매니페스트(JSONL) 내보내기")
    m.add_argument("--out", default=str(ROOT / "data" / "manifest.jsonl"))
    m.add_argument("--all", action="store_true", help="수집 실패/대기 문서도 포함")
    m.set_defaults(func=_manifest)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
