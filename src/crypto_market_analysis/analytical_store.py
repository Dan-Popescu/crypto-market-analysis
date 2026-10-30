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
from functools import lru_cache
from pathlib import Path

from .evidence import canonical_json
from .models import Candle
from .storage import read_database_if_present

FORMAT = "observer-candle-snapshot-v1"
COLUMNS = ("id", "source", "asset", "quote", "timeframe", "open_time", "close_time",
           "observed_at", "data_mode", "fingerprint", "payload")
MAX_EXPORT_ROWS = 10_000_000
MAX_QUERY_ROWS = 100_000


def _canonical(value):
    return canonical_json(value)


def _file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _schema():
    import pyarrow as pa

    return pa.schema([(name, pa.int64() if name in ("id", "open_time", "close_time", "observed_at") else pa.string())
                      for name in COLUMNS])


def _snapshot_fingerprint(root):
    """Cheap change detection for already-verified immutable local artifacts."""
    result = []
    try:
        for path in (root, root / "manifest.json", root / "candles.parquet"):
            info = path.lstat()
            expected_type = stat.S_ISDIR if path == root else stat.S_ISREG
            if not expected_type(info.st_mode):
                raise ValueError("Snapshot paths must be regular files in a real directory, without symbolic links.")
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    except OSError as exc:
        raise ValueError(f"Candle snapshot is missing or unreadable: {exc}") from exc
    return tuple(result)


@lru_cache(maxsize=4)
def _verified_snapshot(path, fingerprint):
    store = AnalyticalCandleStore(path)
    if store._fingerprint != fingerprint:
        raise ValueError("Candle snapshot changed while it was being opened.")
    return store


def open_analytical_snapshot(snapshot_directory: Path | str) -> AnalyticalCandleStore:
    """Reuse at most four fully verified immutable snapshots across requests.

    A changed inode, size, modification time or change time forces verification.
    Query methods also check these local file identities before reading. This is
    an immutability cache, not authentication against a malicious filesystem.
    """
    root = Path(snapshot_directory).expanduser().absolute()
    return _verified_snapshot(str(root), _snapshot_fingerprint(root))


def export_candle_snapshot(store, output_directory: Path | str, *, batch_rows: int = 4096,
                           max_rows: int = MAX_EXPORT_ROWS) -> dict:
    """Atomically publish a new directory from one read-only SQLite transaction.

    No target is overwritten. Row ids and payloads are retained losslessly, and
    a failed or over-budget export removes its temporary directory. RAM is bound
    by the batch size (individual values remain bounded by SQLite's value limit).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if type(batch_rows) is not int or not 1 <= batch_rows <= 16384:
        raise ValueError("batch_rows must be between 1 and 16384.")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_EXPORT_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_EXPORT_ROWS}.")
    target = Path(output_directory).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError("Snapshot output must be a new directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        parquet = staging / "candles.parquet"
        content_hash = hashlib.sha256()
        count = 0
        with read_database_if_present(store.path) as con:
            if con is None:
                raise ValueError("Cannot export a missing SQLite store.")
            con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
            watermark = con.execute("SELECT COALESCE(MAX(id),0) FROM candles").fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            if total > max_rows:
                raise ValueError("Candle snapshot exceeds its row budget.")
            cursor = con.execute(f"SELECT {','.join(COLUMNS)} FROM candles ORDER BY asset,source,quote,timeframe,open_time,data_mode,observed_at,id")
            with pq.ParquetWriter(parquet, _schema(), compression="zstd") as writer:
                rows, batch_bytes = [], 0
                for raw in cursor:
                    row = dict(raw)
                    encoded = (_canonical(row) + "\n").encode()
                    content_hash.update(encoded)
                    rows.append(row)
                    batch_bytes += len(encoded)
                    if len(rows) >= batch_rows or batch_bytes >= 8 * 1024 * 1024:
                        writer.write_table(pa.Table.from_pylist(rows, schema=_schema()))
                        count += len(rows)
                        rows, batch_bytes = [], 0
                if rows:
                    writer.write_table(pa.Table.from_pylist(rows, schema=_schema()))
                    count += len(rows)
        manifest = {"format": FORMAT, "created_at": int(time.time()), "table": "candles",
                    "file": "candles.parquet", "rows": count, "sqlite_max_id": watermark,
                    "columns": list(COLUMNS), "all_vintages": True,
                    "version_order": ["observed_at DESC", "id DESC"],
                    "content_order": ["asset", "source", "quote", "timeframe", "open_time", "data_mode", "observed_at", "id"],
                    "content_sha256": content_hash.hexdigest(), "file_sha256": _file_hash(parquet),
                    "file_bytes": parquet.stat().st_size}
        (staging / "manifest.json").write_text(_canonical(manifest) + "\n", encoding="utf-8")
        # rename is atomic on the target filesystem. Fail if a directory appeared.
        if target.exists() or target.is_symlink():
            raise ValueError("Snapshot output appeared during export.")
        os.rename(staging, target)
        return manifest
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

        root = Path(snapshot_directory).expanduser().absolute()
        fingerprint = _snapshot_fingerprint(root)
        self.path = root / "candles.parquet"
        manifest_path = root / "manifest.json"
        if root.is_symlink() or self.path.is_symlink() or manifest_path.is_symlink():
            raise ValueError("Snapshot paths must not be symbolic links.")
        try:
            if manifest_path.stat().st_size > 64 * 1024:
                raise ValueError("Snapshot manifest exceeds its size budget.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
                    or manifest.get("file") != "candles.parquet" or manifest.get("columns") != list(COLUMNS)
                    or manifest.get("all_vintages") is not True
                    or type(manifest.get("rows")) is not int or not 0 <= manifest["rows"] <= MAX_EXPORT_ROWS
                    or type(manifest.get("sqlite_max_id")) is not int or manifest["sqlite_max_id"] < 0):
                raise ValueError("Unsupported candle snapshot manifest.")
            if self.path.stat().st_size != manifest.get("file_bytes") or _file_hash(self.path) != manifest.get("file_sha256"):
                raise ValueError("Candle snapshot file checksum mismatch.")
            parquet = pq.ParquetFile(self.path)
            if parquet.schema_arrow != _schema() or parquet.metadata.num_rows != manifest["rows"]:
                raise ValueError("Candle snapshot schema or row count mismatch.")
            content_hash = hashlib.sha256()
            for batch in parquet.iter_batches(batch_size=64):
                for row in batch.to_pylist():
                    content_hash.update((_canonical(row) + "\n").encode())
            if content_hash.hexdigest() != manifest.get("content_sha256"):
                raise ValueError("Candle snapshot source-content checksum mismatch.")
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Candle snapshot is missing or unreadable: {exc}") from exc
        self.manifest = manifest
        if _snapshot_fingerprint(root) != fingerprint:
            raise ValueError("Candle snapshot changed during verification.")
        self._fingerprint = fingerprint

    def _selection(self, assets, start, end, as_of, source, quote, mode, timeframe, max_id=None):
        if _snapshot_fingerprint(self.path.parent) != self._fingerprint:
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
        return query, [str(self.path), *values]

    def candles(self, assets: list[str], *, start: int, end: int, as_of: int,
                source: str | None = None, quote: str | None = None, mode: str = "live",
                timeframe: str = "1h", max_id: int | None = None, max_rows: int = 10000) -> list[Candle]:
        """Select latest receipts with start <= open_time < end <= as_of.

        Native source, quote, timeframe and mode remain separate identities. The
        optional SQLite max_id pins an earlier receipt fence within this export.
        SQL applies the result limit before any payload is decoded into Python.
        """
        import duckdb

        selection, values = self._selection(assets, start, end, as_of, source, quote, mode, timeframe, max_id)
        if type(max_rows) is not int or not 1 <= max_rows <= MAX_QUERY_ROWS:
            raise ValueError(f"max_rows must be between 1 and {MAX_QUERY_ROWS}.")
        query = selection + """, selected AS (SELECT payload FROM latest
            ORDER BY asset,source,quote,timeframe,open_time,data_mode LIMIT ?)
            SELECT CASE WHEN SUM(octet_length(encode(payload))) OVER() > 67108864
                THEN error('Analytical candle query exceeds its 64 MiB payload budget') ELSE payload END FROM selected"""
        with duckdb.connect(config={"threads": "2", "memory_limit": "256MB"}) as con:
            try:
                rows = con.execute(query, [*values, max_rows + 1]).fetchall()
            except duckdb.InvalidInputException as exc:
                raise ValueError(str(exc)) from exc
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
        import duckdb

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
        with duckdb.connect(config={"threads": "2", "memory_limit": "256MB"}) as con:
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
                "snapshot": {key: self.manifest[key] for key in ("format", "created_at", "rows", "sqlite_max_id", "file_sha256", "content_sha256")},
                "expires_at": None,
                "coverage_note": "Missing hours refer to the requested interval; absent assets remain unknown. Immutable candle export, not live or full-dossier coverage."}


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
    if manifest.is_symlink() or manifest.stat().st_size > 65536:
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
    return open_analytical_snapshot(snapshot_path(settings, name)).fleet_summary(**values)
