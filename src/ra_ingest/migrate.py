"""One-off migrations between storage setups.

  migrate_paths  rows written before object-store keys hold absolute file paths
                 (document_versions.storage_path, prep_documents.output_path); rewrite the
                 ones inside the store root, or written on another machine under a
                 ".../data/raw|parsed/" folder, to keys ("raw/fda/ab/<sha>.pdf", "parsed/<sha>.json")
  copy_database  copy every table from one database to another (SQLite -> PostgreSQL),
                 rewriting legacy paths on the way and resetting PostgreSQL id sequences
"""
from __future__ import annotations

import os
import re
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


# A path written on another machine (".../data/raw/fda/ab/<sha>.pdf"): the part after data/ is the key.
_FOREIGN_DATA_PATH = re.compile(r"^.*[/\\]data[/\\]((?:raw|parsed)[/\\].+)$")


def _to_key(value, store: ObjectStore):
    if not isinstance(value, str):
        return value
    if os.path.isabs(value):
        key = store.key_for(value)
        if key:
            return key
    m = _FOREIGN_DATA_PATH.match(value)
    return m.group(1).replace("\\", "/") if m else value


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
                    # "/x/y" is not absolute on Windows (Python 3.13), but it is a legacy path
                    outside += os.path.isabs(value) or value.startswith(("/", "\\"))
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


# tables whose rows are keyed by a document version's sha256
_SHA_TABLES = ("prep_documents", "page_routes", "index_documents")


def rekey_html_versions(db_url: str, store: ObjectStore, dry_run: bool = False,
                        size_tolerance: int = 64, log=print) -> dict:
    """Move HTML versions recorded under the old fingerprint (version 1: it contained the
    day of the request) to the current one, so the next fetch of the same page is "no change".

    For a document whose newest version is HTML and present in the store:
      * one version: its sha256 is replaced by the current fingerprint of the stored bytes;
      * several versions: an older version that is the same page keeps the row (the parse
        output and the index refer to it) and the newest row is removed. "Same page" =
        same current fingerprint when the older bytes are in the store, else a byte size
        within `size_tolerance` (those pages differed only by the date / script token).
    sha256 is rewritten in prep_documents, page_routes, index_documents and the parse output
    key (parsed/<sha>.json) follows. Nothing is deleted from the store except the key of a
    removed duplicate version."""
    import json

    from sqlalchemy.orm import Session

    from .db import DocumentRow, DocumentVersionRow
    from .objstore import LocalStore
    from .storage import RawStore, fingerprint

    load_all_tables()
    engine = create_engine(db_url, future=True)
    tables = Base.metadata.tables
    stats = {"rekeyed": 0, "duplicates_removed": 0, "unchanged": 0, "skipped_no_raw": 0, "unresolved": 0}
    with Session(engine) as s:
        for doc in s.scalars(select(DocumentRow).order_by(DocumentRow.id)):
            vs = list(s.scalars(select(DocumentVersionRow).where(DocumentVersionRow.document_id == doc.id)
                                .order_by(DocumentVersionRow.id)))
            if not vs or not vs[-1].storage_path.lower().endswith((".html", ".htm")):
                continue
            newest = vs[-1]
            ext = "." + newest.storage_path.rsplit(".", 1)[-1].lower()
            if not store.exists(newest.storage_path):
                stats["skipped_no_raw"] += 1
                continue
            content = store.get_bytes(newest.storage_path)
            new_sha = fingerprint(content, ext)
            keep = None
            for v in reversed(vs[:-1]):
                if store.exists(v.storage_path):
                    same = fingerprint(store.get_bytes(v.storage_path), ext) == new_sha
                else:
                    same = abs((v.size_bytes or 0) - (newest.size_bytes or 0)) <= size_tolerance
                if same:
                    keep = v
                    break
            if keep is None and len(vs) == 1:
                keep = newest
            if keep is None:
                stats["unresolved"] += 1
                continue
            if keep.sha256 == new_sha and keep is newest:
                stats["unchanged"] += 1
                continue
            new_key = RawStore.key(doc.agency, new_sha, ext)
            old_sha = keep.sha256
            log(f"  doc {doc.id} ({doc.agency} {doc.doc_type}): {old_sha[:10]} -> {new_sha[:10]}"
                f"{'' if keep is newest else f' (drops version {newest.id})'}")
            stats["rekeyed"] += 1
            if dry_run:
                continue
            if not store.exists(new_key):
                store.put_bytes(new_key, content)
            if keep is not newest:
                stale = newest.storage_path
                s.delete(newest)
                s.flush()
                stats["duplicates_removed"] += 1
                if stale != new_key and isinstance(store, LocalStore):
                    try:
                        store.path(stale).unlink()
                    except OSError:
                        pass
            keep.sha256, keep.storage_path, keep.size_bytes = new_sha, new_key, len(content)
            s.flush()
            for tname in _SHA_TABLES:
                t = tables.get(tname)
                if t is not None:
                    s.execute(update(t).where(t.c.sha256 == old_sha).values(sha256=new_sha))
            pt = tables.get("prep_documents")
            if pt is not None:
                for row_id, out in s.execute(select(pt.c.id, pt.c.output_path)
                                             .where(pt.c.sha256 == new_sha, pt.c.output_path.is_not(None))).all():
                    if out and out.endswith(f"{old_sha}.json") and store.exists(out):
                        parsed = json.loads(store.read_text(out))
                        parsed["sha256"] = new_sha
                        key = f"parsed/{new_sha}.json"
                        store.put_bytes(key, json.dumps(parsed, ensure_ascii=False).encode("utf-8"))
                        s.execute(update(pt).where(pt.c.id == row_id).values(output_path=key))
        if not dry_run:
            s.commit()
    return stats
