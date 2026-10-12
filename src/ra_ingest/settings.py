"""Where the database and the object store are. Both CLIs (and the later services) read the
same environment variables, so one setting switches every stage:

  RA_DB     SQLAlchemy URL   (default: SQLite data/ra.db; e.g. postgresql+psycopg://ra:ra@localhost:5432/ra)
  RA_STORE  object store     (default: the data/ directory; a shared volume path or s3://bucket/prefix)
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQLITE_DB = f"sqlite:///{ROOT / 'data' / 'ra.db'}"
LOCAL_STORE = str(ROOT / "data")


def default_db() -> str:
    return os.environ.get("RA_DB") or SQLITE_DB


def default_store() -> str:
    return os.environ.get("RA_STORE") or LOCAL_STORE
