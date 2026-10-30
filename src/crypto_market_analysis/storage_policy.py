"""Conservative bounded archives; immutable/reference evidence is never auto-pruned.

Limits are admission budgets, not filesystem quotas. SQLite pages are reusable but
DELETE does not shrink the database. An external quota remains necessary against
other processes, long-lived WAL readers and filesystem allocation overhead.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

_ALLOWED = {
    "book_snapshots": "id source asset quote data_mode captured_at event_time content_hash payload".split(),
    "runs": "id source task observed_at payload".split(),
    "observations": "id source asset metric event_time observed_at data_mode dimension fingerprint payload".split(),
}
_MAX_ARCHIVE_BYTES = 8 * 1024**2


@dataclass(frozen=True)
class StoragePolicy:
    hot_max_bytes: int = 2 * 1024**3
    archive_max_bytes: int = 4 * 1024**3
    archive_max_files: int = 4096
    book_hot_max_bytes: int = 512 * 1024**2
    book_keep_days: int = 7
    run_keep_days: int = 30
    batch_rows: int = 256
    write_reserve_bytes: int = 8 * 1024**2

    def __post_init__(self):
        for key, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer.")
        if self.batch_rows > 256:
            raise ValueError("batch_rows cannot exceed 256.")

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, f"storage_{name}") for name in cls.__dataclass_fields__})


def archive_directory(database: Path) -> Path:
    path = Path(database)
    return path.with_name(path.stem + "-archives")


def _bytes(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def physical_storage(database: Path) -> dict:
    database = Path(database)
    archive = archive_directory(database)
    if archive.is_symlink():
        raise ValueError("Archive directory cannot be a symlink.")
    archived = files = 0
    if archive.exists():
        for path in archive.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("Archive inventory requires regular files only.")
            archived += path.stat().st_size
            files += 1
    sizes = {"database_bytes": _bytes(database), "wal_bytes": _bytes(Path(str(database) + "-wal")),
             "shm_bytes": _bytes(Path(str(database) + "-shm")), "archive_bytes": archived, "archive_files": files}
    sizes["hot_bytes"] = sizes["database_bytes"] + sizes["wal_bytes"] + sizes["shm_bytes"]
    return sizes


def check_write_budget(con, database: Path, *, policy: StoragePolicy | None = None,
                       payload_bytes: int = 0) -> None:
    """Serialize admission with the SQLite writer; reserve WAL/index amplification.

    The 8x payload estimate and fixed reserve are conservative admission estimates,
    not an exact bound on SQLite allocation. Call before a bounded write batch.
    """
    policy = policy or StoragePolicy()
    if not getattr(getattr(con, "con", con), "in_transaction", False):
        con.execute("BEGIN IMMEDIATE")
    sizes = physical_storage(database)
    if sizes["archive_bytes"] > policy.archive_max_bytes or sizes["archive_files"] > policy.archive_max_files:
        raise ValueError("Archive byte budget exceeded; collection refused without deleting evidence.")
    estimate = policy.write_reserve_bytes + payload_bytes * 8
    if sizes["hot_bytes"] + estimate > policy.hot_max_bytes:
        raise ValueError("Hot SQLite/WAL admission budget exceeded; archive/maintenance or increase an explicit budget.")


def _readonly(database: Path):
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError("Storage inspection requires an existing database.")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def inspect_storage(database: Path, policy: StoragePolicy | None = None) -> dict:
    """Read physical bytes and per-family logical payload bytes without migration."""
    policy = policy or StoragePolicy()
    database = Path(database)
    families = {}
    with closing(_readonly(database)) as con:
        con.execute("BEGIN")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in sorted(tables):
            if not re.fullmatch(r"[a-z_]+", name):
                continue
            columns = {r[1] for r in con.execute(f'PRAGMA table_info("{name}")')}
            payload = next((p for p in ("payload", "record") if p in columns), None)
            clock = next((p for p in ("captured_at", "observed_at", "issued_at") if p in columns), None)
            size = f'SUM(length(CAST("{payload}" AS BLOB)))' if payload else "0"
            bounds = f',MIN("{clock}"),MAX("{clock}")' if clock else ",NULL,NULL"
            row = con.execute(f'SELECT COUNT(*),COALESCE({size},0){bounds} FROM "{name}"').fetchone()
            families[name] = dict(zip(("rows", "payload_bytes", "oldest_available_at", "newest_available_at"), row, strict=True))
    directory = archive_directory(database)
    archives = []
    total_archives = 0
    if directory.exists():
        for path in directory.iterdir():
            if path.suffix == ".json":
                total_archives += 1
                if len(archives) < 128:
                    archives.append({"name": path.name, "bytes": path.stat().st_size})
    return {"policy": asdict(policy), "physical": physical_storage(database), "families": families,
            "archives": archives, "archives_total": total_archives, "archives_truncated": total_archives > len(archives),
            "archive_directory": str(directory),
            "automatic_prune_families": ["book_snapshots", "runs"],
            "protected": ["candles", "assessment_journal", "assessment_outcomes", "named_dataset_payloads"],
            "limits": {"book_budget": "logical_UTF8_payload_bytes; physical_SQLite_and_indexes_also_counted_in_hot_budget",
                       "kind": "admission_budgets_not_filesystem_quota", "deleted_pages": "reused_not_reclaimed",
                       "wal": "long_readers_and_external_writers_require_filesystem_quota",
                       "archive_expiry": "never_automatic; full_budget_refuses_further_archives"}}


def _encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def verify_archive(path: Path) -> dict:
    """Bounded read verifies file hash, row schemas and retained-book content hashes."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid archive file or byte bound.")
    encoded = path.read_bytes()
    digest = hashlib.sha256(encoded).hexdigest()
    if path.name not in (digest + ".json", digest + ".pending"):
        raise ValueError("Archive checksum mismatch.")
    try:
        document = json.loads(encoded)
        family, rows = document["family"], document["rows"]
        if (document["schema_version"] != 1 or family not in _ALLOWED or not isinstance(rows, list)
                or not 1 <= len(rows) <= 256 or any(set(row) != set(_ALLOWED[family]) for row in rows)):
            raise ValueError("Unsupported archive schema.")
        if family == "book_snapshots":
            from .book_snapshots import _validated
            for row in rows:
                book = _validated(json.loads(row["payload"]))
                if any(row[key] != book[key] for key in ("source", "asset", "quote", "data_mode", "captured_at", "event_time", "content_hash")):
                    raise ValueError("Archive book columns disagree with evidence.")
        if family == "observations":
            from .models import Observation
            from .storage import _hash

            for row in rows:
                payload = json.loads(row["payload"])
                Observation(**payload)
                if (any(row[key] != payload[key] for key in ("source", "asset", "metric", "event_time", "observed_at", "data_mode"))
                        or row["fingerprint"] != _hash({key: value for key, value in payload.items() if key != "observed_at"})):
                    raise ValueError("Archive observation columns or fingerprint disagree with evidence.")
        if family == "runs":
            for row in rows:
                payload = json.loads(row["payload"])
                if any(row[key] != payload[key] for key in ("source", "task", "observed_at")):
                    raise ValueError("Archive run columns disagree with evidence.")
        for row in rows:
            if type(row["id"]) is not int or row["id"] < 1 or not isinstance(row["payload"], str):
                raise ValueError("Invalid archived row.")
            json.loads(row["payload"])
        return {"status": "verified", "sha256": digest, "bytes": len(encoded), **document}
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"Invalid archive: {exc}") from exc


@contextmanager
def _archive_lock(database: Path):
    # Separate lock serializes maintainers while allowing collectors to continue.
    with closing(sqlite3.connect(Path(database).with_suffix(".archive-lock.sqlite"), timeout=15)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        yield


def _sync_path(path: Path) -> None:
    """Persist a regular file or directory entry before authorizing hot deletion."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_rows(store, family: str, rows: list[dict], policy: StoragePolicy | None = None) -> Path | None:
    """Durably publish a checked batch before callers may delete captured row IDs.

    Retry uses the same content-addressed filename; interrupted .pending files are
    verified and published on the next attempt. Existing corrupt files fail closed.
    """
    if not rows:
        return None
    policy = policy or getattr(store, "storage_policy", None) or StoragePolicy()
    encoded = _encode({"schema_version": 1, "family": family, "rows": rows})
    if len(encoded) > _MAX_ARCHIVE_BYTES or len(rows) > policy.batch_rows:
        raise ValueError("Archive batch exceeds row or byte bound.")
    digest = hashlib.sha256(encoded).hexdigest()
    directory = archive_directory(store.path)
    with _archive_lock(store.path):
        if directory.is_symlink():
            raise ValueError("Archive directory cannot be a symlink.")
        directory.mkdir(exist_ok=True)
        # The newly created archive directory itself must survive a crash. A
        # child-directory fsync alone does not persist its parent's new entry.
        # Repeat on retries because a preexisting directory may be an unsynced copy.
        _sync_path(directory.parent)
        target, pending = directory / (digest + ".json"), directory / (digest + ".pending")
        if target.exists():
            verify_archive(target)
            _sync_path(target)
            _sync_path(directory)
            return target
        if pending.exists():
            # An interrupted staging write is resumable only while its bytes are
            # an exact prefix of the still-present original evidence batch.
            if pending.is_symlink() or pending.stat().st_size > len(encoded):
                raise ValueError("Invalid interrupted archive staging file.")
            prefix = pending.read_bytes()
            if prefix != encoded[:len(prefix)]:
                raise ValueError("Corrupt interrupted archive staging file; hot evidence preserved.")
            missing = encoded[len(prefix):]
            if physical_storage(store.path)["archive_bytes"] + len(missing) > policy.archive_max_bytes:
                raise ValueError("Archive byte budget exhausted; hot evidence preserved.")
            with pending.open("ab") as stream:
                stream.write(missing)
                stream.flush()
                os.fsync(stream.fileno())
            verify_archive(pending)
        else:
            sizes = physical_storage(store.path)
            if sizes["archive_bytes"] + len(encoded) > policy.archive_max_bytes or sizes["archive_files"] >= policy.archive_max_files:
                raise ValueError("Archive byte budget exhausted; hot evidence preserved.")
            with pending.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            verify_archive(pending)
        os.rename(pending, target)
        _sync_path(directory)
        verify_archive(target)
        return target


def _batch(rows, policy):
    result, size = [], 0
    for row in rows:
        candidate = dict(row)
        cost = len(_encode(candidate)) + 1
        if size + cost > _MAX_ARCHIVE_BYTES - 1024:
            break
        result.append(candidate)
        size += cost
        if len(result) == policy.batch_rows:
            break
    return result


def maintain_storage(store, policy: StoragePolicy | None = None, *, apply: bool = False, now: int | None = None) -> dict:
    """Archive/prune at most one bounded batch per eligible family; default dry run."""
    policy = policy or getattr(store, "storage_policy", None) or StoragePolicy()
    now = int(time.time()) if now is None else now
    plan = []
    # Capture one snapshot, then delete only matching captured IDs after publication.
    with closing(_readonly(store.path)) as con:
        con.execute("BEGIN")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for family, clock, days in (("book_snapshots", "captured_at", policy.book_keep_days),
                                    ("runs", "observed_at", policy.run_keep_days)):
            if family not in tables:
                continue
            # Restores assign new local IDs, and demo runs must never displace
            # the latest live clock. Unknown legacy modes remain a separate slot.
            extra = """ AND id NOT IN (SELECT id FROM (
                SELECT id,ROW_NUMBER() OVER (PARTITION BY source,task,
                    CASE WHEN json_extract(payload,'$.data_mode') IN ('live','demo','import')
                    THEN json_extract(payload,'$.data_mode') ELSE 'unknown' END
                    ORDER BY observed_at DESC,id DESC) AS position FROM runs
                ) WHERE position=1)""" if family == "runs" else ""
            rows = con.execute(f'SELECT * FROM "{family}" WHERE "{clock}"<?{extra} ORDER BY "{clock}",id LIMIT ?',
                               (now - days * 86400, policy.batch_rows)).fetchall()
            batch = _batch(rows, policy)
            if batch:
                plan.append((family, batch))
    result = {"apply": apply, "families": [], "policy": asdict(policy)}
    for family, rows in plan:
        item = {"family": family, "eligible_batch_rows": len(rows), "deleted_rows": 0, "archive": None}
        if apply:
            archive = archive_rows(store, family, rows, policy)
            # Verification immediately precedes deletion; external mutation fails closed.
            verify_archive(archive)
            with store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                for row in rows:
                    item["deleted_rows"] += con.execute(f'DELETE FROM "{family}" WHERE id=? AND payload=?',
                                                         (row["id"], row["payload"])).rowcount
            item["archive"] = str(archive)
        result["families"].append(item)
    return result


def restore_archive(path: Path, store, *, apply: bool = False) -> dict:
    """Restore checked evidence idempotently with new local row IDs; no overwrite."""
    verified = verify_archive(path)
    family, rows = verified["family"], verified["rows"]
    inserted = 0
    if apply:
        if family == "book_snapshots":
            from .book_snapshots import retain_books
            inserted = retain_books(store, [json.loads(row["payload"]) for row in rows])
        else:
            with store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                columns = [name for name in _ALLOWED[family] if name != "id"]
                identity = ("source", "task", "observed_at") if family == "runs" else (
                    "source", "asset", "metric", "event_time", "data_mode", "dimension", "observed_at", "fingerprint")
                predicate = " AND ".join(f'"{name}"=?' for name in (*identity, "payload"))
                missing, seen = [], set()
                for row in rows:
                    if row["payload"] in seen:
                        continue
                    seen.add(row["payload"])
                    # Indexed identity predicates bound duplicate checks to one
                    # event/run version instead of scanning every stored payload.
                    if not con.execute(f'SELECT 1 FROM "{family}" WHERE {predicate} LIMIT 1',
                                       [row[name] for name in (*identity, "payload")]).fetchone():
                        missing.append(row)
                if missing:
                    check_write_budget(con, store.path, policy=getattr(store, "storage_policy", None),
                                       payload_bytes=sum(len(row["payload"].encode()) for row in missing))
                for row in missing:
                    con.execute(f'INSERT INTO "{family}" ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                                [row[name] for name in columns])
                    inserted += 1
    return {"apply": apply, "family": family, "verified_rows": len(rows), "inserted_rows": inserted,
            "original_ids": "archive_provenance_only; restored_ids_are_local"}


def read_archived_books(database: Path, *, sources: list[str], asset: str, quote: str, data_mode: str, as_of: int, max_archives: int = 128) -> dict:
    """Explicit archive reader; verifies each bounded archive and preserves unknown clocks."""
    from .book_snapshots import _selectors
    _selectors(sources, asset, quote, data_mode, as_of)
    if type(max_archives) is not int or not 1 <= max_archives <= 4096:
        raise ValueError("max_archives must be from 1 to 4096.")
    selected, scanned, truncated = {}, 0, False
    directory = archive_directory(database)
    if directory.is_symlink():
        raise ValueError("Archive directory cannot be a symlink.")
    paths = directory.glob("*.json")
    for path in paths:
        if scanned == max_archives:
            truncated = True
            break
        scanned += 1
        archive = verify_archive(path)
        if archive["family"] != "book_snapshots":
            continue
        for row in archive["rows"]:
            if (row["source"] in sources and row["asset"] == asset and row["quote"] == quote
                    and row["data_mode"] == data_mode and row["captured_at"] <= as_of
                    and (row["event_time"] is None or row["event_time"] <= as_of)):
                prior = selected.get(row["source"])
                if prior is None or (row["captured_at"], row["id"]) > (prior["captured_at"], prior["id"]):
                    selected[row["source"]] = row
    return {"books": [json.loads(selected[source]["payload"]) for source in sources if source in selected],
            "storage_tier": "archive", "as_of": as_of,
            "coverage": {"archives_scanned": scanned, "archives_truncated": truncated,
                         "selection_complete": not truncated}}
