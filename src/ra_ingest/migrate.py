"""One-off migrations between storage setups.

  migrate_paths  rows written before object-store keys hold absolute file paths
                 (document_versions.storage_path, prep_documents.output_path); rewrite the
                 ones inside the store root to keys ("raw/fda/ab/<sha>.pdf", "parsed/<sha>.json")
  copy_database  copy every table from one database to another (SQLite -> PostgreSQL),
                 rewriting legacy paths on the way and resetting PostgreSQL id sequences
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, inspect, select, text, update

from .db import Base, Store
from .objstore import ObjectStore


def load_all_tables() -> None:
    """Register the tables of the later stages in the shared metadata (when installed)."""
    import importlib

    for mod in ("ra_prep.store", "ra_chunk.store"):
        try:
            importlib.import_module(mod)
        except ImportError:
            pass


# (table, column) pairs that hold a stored object's location
PATH_COLUMNS = [("document_versions", "storage_path"), ("prep_documents", "output_path")]


def _to_key(value, store: ObjectStore):
    if isinstance(value, str) and os.path.isabs(value):
        return store.key_for(value) or value
    return value


def migrate_paths(db_url: str, store: ObjectStore, dry_run: bool = False) -> dict:
    """Rewrite absolute paths inside the store root to keys. Paths outside it are kept
    (still readable through ObjectStore.resolve) and counted as 'outside'."""
    load_all_tables()
    st = Store(db_url)
    tables = Base.metadata.tables
    stats: dict = {}
    with st.engine.begin() as conn:
        for tname, col in PATH_COLUMNS:
            if tname not in tables or not inspect(conn).has_table(tname):
                continue
            t = tables[tname]
            c = t.c[col]
            done = outside = 0
            for row_id, value in conn.execute(select(t.c.id, c).where(c.is_not(None))).all():
                new = _to_key(value, store)
                if new == value:
                    outside += os.path.isabs(value)
                    continue
                done += 1
                if not dry_run:
                    conn.execute(update(t).where(t.c.id == row_id).values({col: new}))
            stats[f"{tname}.{col}"] = {"rewritten": done, "outside_store": outside}
    return stats


def _aware(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)  # SQLite drops the zone; values were written in UTC
    return v


def copy_database(src_url: str, dst_url: str, store: ObjectStore | None = None,
                  batch: int = 1000, log=print) -> dict:
    """Copy all rows from src to an empty dst (schema created by the same models)."""
    load_all_tables()
    src = create_engine(src_url, future=True)
    Store(dst_url)  # creates the schema (and the vector extension when ra_chunk is installed)
    dst = create_engine(dst_url, future=True)
    src_tables = set(inspect(src).get_table_names())
    path_cols = {t: c for t, c in PATH_COLUMNS}
    counts: dict[str, int] = {}
    with src.connect() as sc, dst.begin() as dc:
        for t in Base.metadata.sorted_tables:
            if t.name in src_tables and dc.execute(select(func.count()).select_from(t)).scalar():
                raise SystemExit(f"destination table {t.name} is not empty; copy into a fresh database")
        for t in Base.metadata.sorted_tables:
            if t.name not in src_tables:
                continue
            src_cols = {c["name"] for c in inspect(src).get_columns(t.name)}
            cols = [c for c in t.columns if c.name in src_cols]
            # self-references (documents.parent_document_id): insert as NULL, set afterwards
            selfref = [fk.parent.name for fk in t.foreign_keys if fk.column.table is t]
            later: list[tuple] = []
            n = 0
            rows = sc.execute(select(*cols).order_by(*t.primary_key.columns))
            while True:
                chunk = rows.fetchmany(batch)
                if not chunk:
                    break
                out = []
                for r in chunk:
                    d = {k: _aware(v) for k, v in r._mapping.items()}
                    if store is not None and t.name in path_cols and d.get(path_cols[t.name]):
                        d[path_cols[t.name]] = _to_key(d[path_cols[t.name]], store)
                    for k in selfref:
                        if d.get(k) is not None:
                            later.append((d["id"], k, d[k]))
                            d[k] = None
                    out.append(d)
                dc.execute(t.insert(), out)
                n += len(out)
            for row_id, k, v in later:
                dc.execute(update(t).where(t.c.id == row_id).values({k: v}))
            counts[t.name] = n
            log(f"  {t.name}: {n}")
        if dst.dialect.name == "postgresql":
            for t in Base.metadata.sorted_tables:
                pk = list(t.primary_key.columns)
                if t.name in counts and len(pk) == 1 and pk[0].autoincrement is not False \
                        and pk[0].type.python_type is int:
                    dc.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{t.name}', '{pk[0].name}'), "
                        f"COALESCE(MAX({pk[0].name}), 1), MAX({pk[0].name}) IS NOT NULL) FROM {t.name}"))
    return counts

