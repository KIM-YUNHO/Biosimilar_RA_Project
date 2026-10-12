"""Run the whole pipeline on a local machine: ingest -> model warmup -> preprocessing ->
chunking + embedding into the index -> status.

Works on Windows, macOS and Linux (only calls this Python's own modules).
Every step is resumable: ingestion keeps what it already fetched (content-addressed store)
preprocessing skips document versions already parsed by the same engine version, and
indexing skips documents whose index already matches (version, engine, chunker, model).

Examples
  python scripts/run_local.py                         # ustekinumab, 4 biosimilars + guidance
  python scripts/run_local.py --skip-ingest --workers 2
  python scripts/run_local.py --products SB17 --agencies ema,fda,hc
  python scripts/run_local.py --products BAT2206 --include-holdout   # new-product timing (P7)
  python scripts/run_local.py --device cuda --workers 1               # NVIDIA GPU
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCTS = ["SB17", "ABP654", "CT-P43", "BMAB1200"]
# documents that answer most questions first, so a stopped run is still useful
PRIORITY_TYPES = [
    "assessment_report,review,sbd,rds",
    "label,product_monograph,product_information,approval_letter,epar_overview,procedural_steps",
    None,  # everything else that selection keeps
]


def step(title: str, args: list[str]) -> None:
    print(f"\n=== {title}\n$ {' '.join(args)}", flush=True)
    t = time.time()
    r = subprocess.run(args, cwd=ROOT)
    print(f"--- {title}: exit {r.returncode}, {time.time() - t:.0f}s", flush=True)
    if r.returncode not in (0,):
        # ingest/run return 1 when some documents failed; keep going, failures are recorded
        print("    (some items failed; see the log above and `ra-prep status`)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ingredient", default="ustekinumab")
    ap.add_argument("--products", nargs="*", default=DEFAULT_PRODUCTS,
                    help="dev codes or brand names (default: the 4 non-holdout biosimilars)")
    ap.add_argument("--agencies", default="ema,fda,hc")
    ap.add_argument("--include-holdout", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--skip-guidance", action="store_true")
    ap.add_argument("--skip-index", action="store_true", help="stop after preprocessing")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    ap.add_argument("--db", help="SQLAlchemy URL (default: RA_DB or SQLite data/ra.db)")
    ap.add_argument("--store", help="object store: directory or s3://bucket/prefix (default: RA_STORE or data/)")
    a = ap.parse_args()

    py = sys.executable
    db = (["--db", a.db] if a.db else []) + (["--store", a.store] if a.store else [])
    prods = [x for p in a.products for x in ("--product", p)]

    if not a.skip_ingest:
        holdout = ["--include-holdout"] if a.include_holdout else []
        step("ingest", [py, "-m", "ra_ingest", *db, "ingest", "--ingredient", a.ingredient,
                        "--agencies", f"{a.agencies},guidance,ctgov", *holdout])
    step("warmup (models + smoke test)", [py, "-m", "ra_prep", "warmup", "--device", a.device])

    run = [py, "-m", "ra_prep", *db, "run", "--workers", str(a.workers), "--device", a.device]
    if not a.skip_guidance:
        step("preprocess guidance", [*run, "--guidance"])
    for types in PRIORITY_TYPES:
        extra = ["--doc-types", types] if types else []
        step(f"preprocess products ({types or 'remaining'})",
             [*run, *prods, "--agencies", a.agencies, *extra])
    step("status", [py, "-m", "ra_prep", *db, "status"])
    if not a.skip_index:  # everything parsed so far: chunks -> bge-m3 -> index
        step("index", [py, "-m", "ra_chunk", *db, "run", "--device", a.device])
        step("index status", [py, "-m", "ra_chunk", *db, "status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
