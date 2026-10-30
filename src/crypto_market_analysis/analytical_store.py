"""Explicit immutable candle snapshots for bounded multi-asset DuckDB research.

SQLite remains authoritative. This candle-only export is neither a full dossier
snapshot nor a backup; it preserves all vintages instead of collapsing revisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from threading import BoundedSemaphore, Event, Timer

from .evidence import canonical_json
from .models import Candle
from .storage import read_database_if_present

FORMAT = "observer-candle-snapshot-v1"
PARTITIONED_FORMAT = "observer-candle-snapshot-v2"
COLUMNS = ("id", "source", "asset", "quote", "timeframe", "open_time", "close_time",
           "observed_at", "data_mode", "fingerprint", "payload")
MAX_EXPORT_ROWS = 10_000_000
MAX_PARTITIONED_ROWS = 100_000_000
MAX_ROWS_PER_FILE = 1_000_000
MAX_PARTITIONS = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_QUERY_ROWS = 100_000
_QUERY_SECONDS = 30
_VERIFICATION_SECONDS = 30
_QUERY_MEMORY = "256MB"
_QUERY_SLOTS = BoundedSemaphore(2)
_VERIFICATION_SLOTS = BoundedSemaphore(1)


def _canonical(value):
    return canonical_json(value)


def _file_hash(path, check_budget=lambda: None):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_budget()
            result.update(chunk)
    return result.hexdigest()


def _schema():
    import pyarrow as pa

    return pa.schema([(name, pa.int64() if name in ("id", "open_time", "close_time", "observed_at") else pa.string())
                      for name in COLUMNS])


def _read_manifest(root):
    path = root / "manifest.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Snapshot manifest is unsafe or exceeds its size budget.")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Candle snapshot is missing or unreadable: {exc}") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") not in (FORMAT, PARTITIONED_FORMAT)
            or manifest.get("columns") != list(COLUMNS) or manifest.get("all_vintages") is not True
            or type(manifest.get("rows")) is not int or not 0 <= manifest["rows"] <= MAX_PARTITIONED_ROWS
            or type(manifest.get("sqlite_max_id")) is not int or manifest["sqlite_max_id"] < 0):
        raise ValueError("Unsupported candle snapshot manifest.")
    if manifest["format"] == FORMAT:
        if manifest.get("file") != "candles.parquet" or manifest["rows"] > MAX_EXPORT_ROWS:
            raise ValueError("Unsupported candle snapshot manifest.")
        files = [{"file": "candles.parquet", "rows": manifest["rows"],
                  "file_bytes": manifest.get("file_bytes"), "file_sha256": manifest.get("file_sha256")}]
    else:
        files = manifest.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_PARTITIONS:
            raise ValueError("Partitioned candle snapshot has an invalid file count.")
        for index, part in enumerate(files, 1):
            if (not isinstance(part, dict) or part.get("file") != f"candles-{index:06d}.parquet"
                    or type(part.get("rows")) is not int or not 0 <= part["rows"] <= MAX_ROWS_PER_FILE):
                raise ValueError("Partitioned candle snapshot has an unsafe file or row count.")
        if sum(part["rows"] for part in files) != manifest["rows"]:
            raise ValueError("Partitioned candle snapshot total row count mismatch.")
        if hashlib.sha256(_canonical(files).encode()).hexdigest() != manifest.get("file_sha256"):
            raise ValueError("Partitioned candle snapshot file-list checksum mismatch.")
    return manifest, files


def _snapshot_fingerprint(root, files=None):
    """Cheap change detection for already-verified immutable local artifacts."""
    result = []
    if files is None:
        _, files = _read_manifest(root)
    try:
        for path in (root, root / "manifest.json", *(root / part["file"] for part in files)):
            info = path.lstat()
            expected_type = stat.S_ISDIR if path == root else stat.S_ISREG
            if not expected_type(info.st_mode):
                raise ValueError("Snapshot paths must be regular files in a real directory, without symbolic links.")
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    except OSError as exc:
        raise ValueError(f"Candle snapshot is missing or unreadable: {exc}") from exc
    return tuple(result)


@contextmanager
def analytical_query_connection():
    """Bound SQL memory and time without creating spill files in a read path."""
    import duckdb

    if not _QUERY_SLOTS.acquire(blocking=False):
        raise ValueError("Analytical read capacity is busy; retry after another fleet query finishes.")
    expired = Event()
    try:
        with duckdb.connect(config={"threads": "2", "memory_limit": _QUERY_MEMORY, "temp_directory": ""}) as con:
            def interrupt():
                expired.set()
                try:
                    con.interrupt()
                except duckdb.Error:
                    pass

            timer = Timer(_QUERY_SECONDS, interrupt)
            timer.daemon = True
            timer.start()
            try:
                yield con
                if expired.is_set():
                    raise ValueError(f"Analytical query exceeded its {_QUERY_SECONDS}-second deadline; narrow the assets/time interval.")
            finally:
                timer.cancel()
                timer.join()
    except duckdb.Error as exc:
        reason = f"{_QUERY_SECONDS}-second deadline" if expired.is_set() else "read-only SQL budget"
        raise ValueError(f"Analytical query exceeded its {reason} or failed: {exc}. Narrow the assets/time interval or export a smaller slice.") from exc
    finally:
        _QUERY_SLOTS.release()


# Compatibility for callers of the original internal name. New analytical
# owners share this public resource boundary and its process-wide budgets.
_query_connection = analytical_query_connection


@lru_cache(maxsize=4)
def _verified_snapshot(path, fingerprint):
    if not _VERIFICATION_SLOTS.acquire(blocking=False):
        raise ValueError("Snapshot verification is busy; retry after the current verification finishes.")
    try:
        store = AnalyticalCandleStore(path)
        if store._fingerprint != fingerprint:
            raise ValueError("Candle snapshot changed while it was being opened.")
        return store
    finally:
        _VERIFICATION_SLOTS.release()


def open_analytical_snapshot(snapshot_directory: Path | str) -> AnalyticalCandleStore:
    """Reuse at most four fully verified immutable snapshots across requests.

    A changed inode, size, modification time or change time forces verification.
    Query methods also check these local file identities before reading. This is
    an immutability cache, not authentication against a malicious filesystem.
    """
    root = Path(snapshot_directory).expanduser().absolute()
    return _verified_snapshot(str(root), _snapshot_fingerprint(root))


def _export_selection(assets, source, quote, mode, start, end):
    conditions, values = [], []
    if assets is not None:
        if (not isinstance(assets, list) or not 1 <= len(assets) <= 1000
                or any(not isinstance(asset, str) or not asset or len(asset) > 64 for asset in assets)):
            raise ValueError("Export assets must contain 1..1000 nonempty symbols of at most 64 characters.")
        assets = sorted(set(assets))
        conditions.append("asset IN (" + ",".join("?" for _ in assets) + ")")
        values.extend(assets)
    for name, value in (("source", source), ("quote", quote), ("data_mode", mode)):
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError(f"Export {name} must be a nonempty string.")
            conditions.append(f"{name}=?")
            values.append(value)
    if mode is not None and mode not in ("live", "demo", "import"):
        raise ValueError("Unsupported export data mode.")
    for name, operator, value in (("start", ">=", start), ("end", "<", end)):
        if value is not None:
            if type(value) is not int:
                raise ValueError(f"Export {name} must be integer UTC epoch seconds.")
            conditions.append(f"open_time{operator}?")
            values.append(value)
    if start is not None and end is not None and start >= end:
        raise ValueError("Export bounds must satisfy start < end.")
    return (" WHERE " + " AND ".join(conditions) if conditions else ""), values, {
        "assets": assets, "source": source, "quote": quote, "mode": mode, "start": start, "end": end}


def export_candle_snapshot(store, output_directory: Path | str, *, batch_rows: int = 4096,
                           max_rows: int = MAX_EXPORT_ROWS, rows_per_file: int | None = None,
                           assets: list[str] | None = None, source: str | None = None,
                           quote: str | None = None, mode: str | None = None,
                           start: int | None = None, end: int | None = None,
                           max_bytes: int = 8 * 1024**3, max_seconds: int = 120) -> dict:
    """Atomically publish a new directory from one read-only SQLite transaction.

    No target is overwritten. Row ids and payloads are retained losslessly, and
    a failed or over-budget export removes its temporary directory. RAM is bound
    by the batch size and an 8 MiB canonical batch threshold. Set rows_per_file
    (at most one million) for a v2 multi-file snapshot, with an explicit total
    ceiling up to 100 million rows. Omitting it preserves v1 single-file output.
    Filters select bars before counting and streaming, retaining every receipt
    for selected bars. Byte/deadline budgets fail atomically; they do not promise
    that a particular asset/history size can be processed on this machine.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if type(batch_rows) is not int or not 1 <= batch_rows <= 16384:
        raise ValueError("batch_rows must be between 1 and 16384.")
    row_limit = MAX_EXPORT_ROWS if rows_per_file is None else MAX_PARTITIONED_ROWS
    if type(max_rows) is not int or not 1 <= max_rows <= row_limit:
        raise ValueError(f"max_rows must be between 1 and {row_limit}; use partitioned output for larger slices.")
    if rows_per_file is not None and (type(rows_per_file) is not int or not 1 <= rows_per_file <= MAX_ROWS_PER_FILE):
        raise ValueError(f"rows_per_file must be between 1 and {MAX_ROWS_PER_FILE}.")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 100 * 1024**3:
        raise ValueError("max_bytes must be between 1 byte and 100 GiB of canonical source rows.")
    if type(max_seconds) is not int or not 1 <= max_seconds <= 3600:
        raise ValueError("max_seconds must be between 1 and 3600.")
    where, parameters, selection = _export_selection(assets, source, quote, mode, start, end)
    deadline = time.monotonic() + max_seconds

    def check_budget():
        if time.monotonic() > deadline:
            raise ValueError("Candle snapshot exceeds its export deadline; narrow the slice or explicitly increase max_seconds.")

    target = Path(output_directory).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError("Snapshot output must be a new directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        content_hash = hashlib.sha256()
        count, source_bytes = 0, 0
        files = []
        with read_database_if_present(store.path) as con:
            if con is None:
                raise ValueError("Cannot export a missing SQLite store.")
            con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
            con.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
            watermark = con.execute("SELECT COALESCE(MAX(id),0) FROM candles").fetchone()[0]
            total = con.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM candles{where} LIMIT ?)", [*parameters, max_rows + 1]).fetchone()[0]
            if total > max_rows:
                raise ValueError("Candle snapshot exceeds its row budget.")
            part_limit = rows_per_file or MAX_EXPORT_ROWS
            if (total + part_limit - 1) // part_limit > MAX_PARTITIONS:
                raise ValueError("Candle snapshot exceeds its partition-count budget; increase rows_per_file or narrow the slice.")
            cursor = con.execute(f"SELECT {','.join(COLUMNS)} FROM candles{where} ORDER BY asset,source,quote,timeframe,open_time,data_mode,observed_at,id", parameters)
            writer, part_rows = None, 0

            def close_part():
                nonlocal writer, part_rows
                if writer is None:
                    return
                writer.close()
                writer = None
                path = staging / (f"candles-{len(files) + 1:06d}.parquet" if rows_per_file is not None else "candles.parquet")
                files.append({"file": path.name, "rows": part_rows, "file_bytes": path.stat().st_size,
                              "file_sha256": _file_hash(path, check_budget)})
                part_rows = 0

            def flush(rows):
                nonlocal writer, part_rows, count
                check_budget()
                if writer is None:
                    name = f"candles-{len(files) + 1:06d}.parquet" if rows_per_file is not None else "candles.parquet"
                    writer = pq.ParquetWriter(staging / name, _schema(), compression="zstd")
                if rows:
                    writer.write_table(pa.Table.from_pylist(rows, schema=_schema()))
                    part_rows += len(rows)
                    count += len(rows)
                if part_rows == part_limit:
                    close_part()

            try:
                rows, batch_bytes = [], 0
                for raw in cursor:
                    check_budget()
                    row = dict(raw)
                    encoded = (_canonical(row) + "\n").encode()
                    source_bytes += len(encoded)
                    if source_bytes > max_bytes:
                        raise ValueError("Candle snapshot exceeds its canonical-source byte budget; narrow the slice or explicitly increase max_bytes.")
                    content_hash.update(encoded)
                    rows.append(row)
                    batch_bytes += len(encoded)
                    if len(rows) >= batch_rows or batch_bytes >= 8 * 1024 * 1024 or part_rows + len(rows) >= part_limit:
                        flush(rows)
                        rows, batch_bytes = [], 0
                if rows:
                    flush(rows)
                elif not files and writer is None:
                    flush([])
                close_part()
            finally:
                if writer is not None:
                    writer.close()
        manifest = {"format": PARTITIONED_FORMAT if rows_per_file is not None else FORMAT,
                    "created_at": int(time.time()), "table": "candles",
                    "rows": count, "sqlite_max_id": watermark,
                    "columns": list(COLUMNS), "all_vintages": True,
                    "version_order": ["observed_at DESC", "id DESC"],
                    "content_order": ["asset", "source", "quote", "timeframe", "open_time", "data_mode", "observed_at", "id"],
                    "content_sha256": content_hash.hexdigest(), "selection": selection,
                    "canonical_source_bytes": source_bytes,
                    "export_budget": {"max_rows": max_rows, "rows_per_file": rows_per_file, "max_bytes": max_bytes, "max_seconds": max_seconds}}
        if rows_per_file is None:
            manifest.update({key: files[0][key] for key in ("file", "file_sha256", "file_bytes")})
        else:
            manifest.update(files=files, file_bytes=sum(part["file_bytes"] for part in files),
                            file_sha256=hashlib.sha256(_canonical(files).encode()).hexdigest(),
                            file_hash_scope="ordered-partition-manifest")
        check_budget()
        (staging / "manifest.json").write_text(_canonical(manifest) + "\n", encoding="utf-8")
        # rename is atomic on the target filesystem. Fail if a directory appeared.
        if target.exists() or target.is_symlink():
            raise ValueError("Snapshot output appeared during export.")
        os.rename(staging, target)
        return manifest
    except sqlite3.OperationalError as exc:
        if time.monotonic() > deadline:
            raise ValueError("Candle snapshot exceeds its export deadline; narrow the slice.") from exc
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


class AnalyticalCandleStore:
    """Verify an immutable export, then query only explicitly bounded candle data.

    The directory must remain immutable throughout this object's lifetime.
    Opening verifies both physical bytes and canonical source-row content. Each
    query owns a separate in-memory DuckDB connection with a 256 MiB memory limit.
    """

    def __init__(self, snapshot_directory: Path | str):
        import pyarrow.parquet as pq

        deadline = time.monotonic() + _VERIFICATION_SECONDS

        def check_verification():
            if time.monotonic() >= deadline:
                raise ValueError("Candle snapshot exceeds its verification deadline; export a narrower slice.")

        root = Path(snapshot_directory).expanduser().absolute()
        check_verification()
        manifest, files = _read_manifest(root)
        fingerprint = _snapshot_fingerprint(root, files)
        self.paths = [root / part["file"] for part in files]
        self.path = self.paths[0]
        try:
            content_hash = hashlib.sha256()
            for part, path in zip(files, self.paths, strict=True):
                if path.stat().st_size != part.get("file_bytes") or _file_hash(path, check_verification) != part.get("file_sha256"):
                    raise ValueError("Candle snapshot file checksum mismatch.")
                parquet = pq.ParquetFile(path)
                if parquet.schema_arrow != _schema() or parquet.metadata.num_rows != part["rows"]:
                    raise ValueError("Candle snapshot schema or row count mismatch.")
                for batch in parquet.iter_batches(batch_size=64):
                    check_verification()
                    for row in batch.to_pylist():
                        content_hash.update((_canonical(row) + "\n").encode())
            if content_hash.hexdigest() != manifest.get("content_sha256"):
                raise ValueError("Candle snapshot source-content checksum mismatch.")
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Candle snapshot is missing or unreadable: {exc}") from exc
        check_verification()
        self.manifest = manifest
        self._files = files
        if _snapshot_fingerprint(root, files) != fingerprint:
            raise ValueError("Candle snapshot changed during verification.")
        self._fingerprint = fingerprint

    def _selection(self, assets, start, end, as_of, source, quote, mode, timeframe, max_id=None):
        if _snapshot_fingerprint(self.path.parent, self._files) != self._fingerprint:
            raise ValueError("Candle snapshot changed after verification; reopen the immutable snapshot.")
        if (not isinstance(assets, list) or not 1 <= len(assets) <= 1000
                or any(not isinstance(asset, str) or not asset or len(asset) > 64 for asset in assets)):
            raise ValueError("assets must contain between 1 and 1000 nonempty symbols of at most 64 characters.")
        if any(type(value) is not int for value in (start, end, as_of)) or not start < end <= as_of:
            raise ValueError("Candle bounds must be integer UTC seconds satisfying start < end <= as_of.")
        if mode not in ("live", "demo", "import"):
            raise ValueError("Unsupported candle data mode.")
        if max_id is not None and (type(max_id) is not int or not 0 <= max_id <= self.manifest["sqlite_max_id"]):
            raise ValueError("max_id must be within the exported SQLite receipt fence.")
        conditions = ["asset IN (" + ",".join("?" for _ in assets) + ")", "open_time>=?", "open_time<?",
                      "close_time<=?", "observed_at<=?", "data_mode=?", "timeframe=?"]
        values = [*assets, start, end, as_of, as_of, mode, timeframe]
        for name, value in (("source", source), ("quote", quote), ("id", max_id)):
            if value is not None:
                conditions.append(f"{name}{'<=' if name == 'id' else '='}?")
                values.append(value)
        query = f"""WITH ranked AS (SELECT *,ROW_NUMBER() OVER(
            PARTITION BY source,asset,quote,timeframe,open_time,data_mode ORDER BY observed_at DESC,id DESC) rn
            FROM read_parquet(?) WHERE {' AND '.join(conditions)}), latest AS (SELECT * FROM ranked WHERE rn=1) """
        return query, [[str(path) for path in self.paths], *values]

    def candles(self, assets: list[str], *, start: int, end: int, as_of: int,
                source: str | None = None, quote: str | None = None, mode: str = "live",
                timeframe: str = "1h", max_id: int | None = None, max_rows: int = 10000) -> list[Candle]:
        """Select latest receipts with start <= open_time < end <= as_of.

        Native source, quote, timeframe and mode remain separate identities. The
        optional SQLite max_id pins an earlier receipt fence within this export.
        SQL applies the result limit before any payload is decoded into Python.
        """
        selection, values = self._selection(assets, start, end, as_of, source, quote, mode, timeframe, max_id)
        if type(max_rows) is not int or not 1 <= max_rows <= MAX_QUERY_ROWS:
            raise ValueError(f"max_rows must be between 1 and {MAX_QUERY_ROWS}.")
        query = selection + """, selected AS (SELECT payload FROM latest
            ORDER BY asset,source,quote,timeframe,open_time,data_mode LIMIT ?)
            SELECT CASE WHEN SUM(octet_length(encode(payload))) OVER() > 67108864
                THEN error('Analytical candle query exceeds its 64 MiB payload budget') ELSE payload END FROM selected"""
        with _query_connection() as con:
            rows = con.execute(query, [*values, max_rows + 1]).fetchall()
        if len(rows) > max_rows:
            raise ValueError("Analytical candle query exceeds its result row budget; narrow the request.")
        return [Candle(**json.loads(row[0])) for row in rows]

    def fleet_summary(self, assets: list[str], *, start: int, end: int, as_of: int,
                      source: str | None = None, quote: str | None = None, mode: str = "live",
                      max_assets: int = 1000, max_series: int = 10000) -> dict:
        """Aggregate one-hour native series in SQL without materializing candles.

        Missing hours describe the requested aligned interval, not an exchange
        listing history. An absent asset has unknown market evidence. This frozen
        export has no expiry; it is never represented as current live coverage.
        """
        selection, values = self._selection(assets, start, end, as_of, source, quote, mode, "1h")
        if type(max_assets) is not int or not 1 <= max_assets <= 1000 or len(set(assets)) > max_assets:
            raise ValueError("Fleet asset count exceeds its configured 1..1000 asset budget.")
        if type(max_series) is not int or not 1 <= max_series <= 10000:
            raise ValueError("max_series must be between 1 and 10000.")
        if start % 3600 or end % 3600:
            raise ValueError("Fleet interval must use aligned one-hour UTC boundaries.")
        expected = (end - start) // 3600
        query = selection + """SELECT asset,source,quote,timeframe,data_mode,COUNT(*) points,
            MIN(open_time) first_open_time,MAX(open_time) latest_open_time,
            MAX(close_time) latest_close_time,arg_max(observed_at,open_time) latest_observed_at,
            arg_max(CAST(json_extract(payload,'$.close') AS DOUBLE),open_time) latest_close
            FROM latest GROUP BY asset,source,quote,timeframe,data_mode
            ORDER BY asset,source,quote,timeframe,data_mode LIMIT ?"""
        with _query_connection() as con:
            cursor = con.execute(query, [*values, max_series + 1])
            names = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        if len(rows) > max_series:
            raise ValueError("Fleet exceeds its native-series budget; narrow source or quote.")
        series = [dict(zip(names, row, strict=True)) for row in rows]
        for row in series:
            row.update(expected_hours=expected, missing_hours=expected - row["points"],
                       coverage_ratio=row["points"] / expected)
        found_assets = {row["asset"] for row in series}
        return {"schema": "observer-analytical-fleet-v1", "assets": sorted(set(assets)),
                "start": start, "end": end, "as_of": as_of, "source": source, "quote": quote,
                "mode": mode, "timeframe": "1h", "expected_hours_per_series": expected,
                "series_count": len(series), "selected_points": sum(row["points"] for row in series),
                "missing_assets": sorted(set(assets) - found_assets), "series": series,
                "snapshot": {**{key: self.manifest[key] for key in ("format", "created_at", "rows", "sqlite_max_id", "file_sha256", "content_sha256")},
                             "selection": self.manifest.get("selection"), "partition_count": len(self.paths)},
                "expires_at": None,
                "resource_limits": {"query_seconds": _QUERY_SECONDS, "query_memory": _QUERY_MEMORY,
                                    "concurrent_queries_per_process": 2, "cold_verification_seconds": _VERIFICATION_SECONDS},
                "coverage_note": "Missing hours refer to this artifact within the requested interval; consult snapshot.selection for export scope. Absent assets remain unknown. Immutable candle export, not live or full-dossier coverage."}

def snapshot_path(settings, name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Snapshot name must contain 1–64 letters, digits, underscores or hyphens.")
    base = settings.data_dir / "analytical_snapshots"
    target = base / name
    if base.is_symlink() or target.is_symlink():
        raise ValueError("Configured snapshot directories must not be symbolic links.")
    return target


def snapshot_status(settings, name="default"):
    target = snapshot_path(settings, name)
    manifest = target / "manifest.json"
    if not manifest.is_file():
        return {"status": "missing", "snapshot_name": name, "read_only": True, "manifest": None}
    if manifest.is_symlink() or manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Invalid or oversized snapshot manifest.")
    return {"status": "present", "snapshot_name": name, "read_only": True,
            "integrity": "Manifest inspection only; analytical reads verify the data.",
            "manifest": json.loads(manifest.read_text(encoding="utf-8"))}


def analytical_fleet(settings, name, request):
    allowed = {"assets", "start", "end", "as_of", "source", "quote", "mode", "max_assets", "max_series"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Unknown analytical fleet request fields.")
    values = dict(request)
    if not {"assets", "start", "end", "as_of"} <= values.keys():
        raise ValueError("Analytical fleet requires assets, start, end and as_of.")
    from .asset_universe import validate_assets

    validate_assets(values["assets"])
    if (any(type(values[key]) is not int for key in ("start", "end", "as_of"))
            or not 0 <= values["start"] < values["end"] <= values["as_of"]
            or values["start"] % 3600 or values["end"] % 3600):
        raise ValueError("Fleet bounds must be aligned UTC hours satisfying start < end <= as_of.")
    target = snapshot_path(settings, name)
    if not target.exists():
        return {"status": "missing", "read_only": True, "snapshot_name": name,
                "assets": values["assets"], "selected_points": None, "series": [],
                "reason": "Export an explicit immutable analytical snapshot before requesting fleet data."}
    return open_analytical_snapshot(target).fleet_summary(**values)
