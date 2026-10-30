"""Explicit all-vintage market-v1 Parquet exports and verified DuckDB readers.

This independent format does not change legacy observer snapshot readers. The
SQLite WAL journal remains authoritative; exports never prune or migrate it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from ..analytical_store import analytical_query_connection
from ..evidence import canonical_json, content_hash
from .storage import MAX_READ_ROWS, _bounded, _epoch, _limit, _mode, _registry_components

FORMAT = "market-v1"
MAX_SNAPSHOT_ROWS = 10_000_000
MAX_FILES = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
VERIFICATION_SECONDS = 30
TABLES = ("registries", "registry_components", "admissions", "calendars", "series", "bars", "actions", "imports", "series_imports")
INTEGER_COLUMNS = {"start", "end", "observed_at", "known_at", "received_at", "declared_at", "effective_at", "bars", "actions"}


def _file_hash(path, deadline):
    fingerprint = hashlib.sha256()
    with path.open("rb") as stream:
        while part := stream.read(1024 * 1024):
            if time.monotonic() > deadline:
                raise ValueError("Market snapshot verification exceeded deadline.")
            fingerprint.update(part)
    return fingerprint.hexdigest()


def _identity(path, directory=False):
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("Market snapshot requires regular files and a real directory, without symlinks.")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def export_market_snapshot(store, directory: Path | str, *, series_ids: list[str] | None = None,
                           data_mode: str | None = None, start: int | None = None, end: int | None = None,
                           max_rows: int = 1_000_000, batch_rows: int = 10_000) -> dict:
    """Freeze all selected vintages from one SQLite read transaction atomically."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _limit(max_rows, MAX_SNAPSHOT_ROWS, "max_rows")
    _limit(batch_rows, 100_000, "batch_rows")
    if series_ids is not None and (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 1000
                                   or any(not isinstance(sid, str) or not sid or len(sid) > 256 for sid in series_ids)):
        raise ValueError("Snapshot selection requires 1..1000 bounded series IDs.")
    if data_mode is not None:
        _mode(data_mode)
    for name, value in (("start", start), ("end", end)):
        if value is not None:
            _epoch(value, name)
    if start is not None and end is not None and start >= end:
        raise ValueError("Snapshot start must precede end.")
    target = Path(directory).absolute()
    if target.exists():
        raise ValueError("Market snapshot requires a new directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    files, schemas, count = [], {}, 0
    deadline = time.monotonic() + 60
    try:
        with store.read_transaction() as con:
            if con is None:
                raise ValueError("Authoritative cross-asset database is missing.")
            conditions, values = [], []
            if series_ids is not None:
                conditions.append("id IN (" + ",".join("?" for _ in series_ids) + ")")
                values.extend(series_ids)
            if data_mode is not None:
                conditions.append("data_mode=?")
                values.append(data_mode)
            selected = con.execute("SELECT id FROM cross_asset_series" + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY id LIMIT 1001", values).fetchall()
            ids = [row[0] for row in selected]
            if len(ids) > 1000:
                raise ValueError("Snapshot exceeds 1000-series fleet budget; select a subset.")
            id_sql = ",".join("?" for _ in ids) or "NULL"
            for table in TABLES:
                base = f"SELECT * FROM cross_asset_{table}"
                table_values = []
                if table == "series":
                    base += f" WHERE id IN ({id_sql})"
                    table_values.extend(ids)
                elif table in ("bars", "actions", "series_imports"):
                    base += f" WHERE series_id IN ({id_sql})"
                    table_values.extend(ids)
                    if table == "bars":
                        if start is not None:
                            base += " AND start>=?"
                            table_values.append(start)
                        if end is not None:
                            base += " AND end<=?"
                            table_values.append(end)
                cursor = con.execute(base + " ORDER BY " + ("id" if table == "series" else "rowid"), table_values)
                columns = [column[0] for column in cursor.description]
                schema = pa.schema([(column, pa.int64() if column in INTEGER_COLUMNS else pa.string()) for column in columns])
                schemas[table] = columns
                part = 0
                pending = None
                exhausted = False
                while True:
                    rows, batch_bytes = [], 0
                    while len(rows) < batch_rows and not exhausted:
                        current = pending if pending is not None else cursor.fetchone()
                        pending = None
                        if current is None:
                            exhausted = True
                            break
                        row_bytes = sum(len(value.encode()) for value in current if isinstance(value, str))
                        if rows and batch_bytes + row_bytes > 32 * 1024 * 1024:
                            pending = current
                            break
                        rows.append(current)
                        batch_bytes += row_bytes
                    if not rows and part:
                        break
                    count += len(rows)
                    if count > max_rows:
                        raise ValueError("Market snapshot exceeds row budget.")
                    if len(files) >= MAX_FILES or time.monotonic() > deadline:
                        raise ValueError("Market snapshot exceeds file/time budget.")
                    part += 1
                    name = f"{table}-{part:06d}.parquet"
                    path = temporary / name
                    pq.write_table(pa.Table.from_pylist([dict(row) for row in rows], schema=schema), path, compression="zstd")
                    size = path.stat().st_size
                    if size > MAX_FILE_BYTES:
                        raise ValueError("Market snapshot part exceeds byte budget; reduce batch_rows.")
                    files.append({"table": table, "file": name, "rows": len(rows), "bytes": size,
                                  "sha256": _file_hash(path, deadline)})
                    if not rows:
                        break
            manifest = {"format": FORMAT, "schema": "market-snapshot-manifest.v1", "all_vintages": True,
                        "rows": count, "tables": schemas, "files": files,
                        "selection": {"series_ids": ids, "data_mode": data_mode, "start": start, "end": end},
                        "receipt_claim": "Import/demo historical clocks are self-declared; live receipts are local journal evidence."}
            manifest["content_sha256"] = content_hash(manifest)
            manifest_path = temporary / "manifest.json"
            manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
            if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("Market snapshot manifest exceeds byte budget.")
        # rename does not overwrite a nonempty existing export directory.
        if target.exists():
            raise ValueError("Market snapshot requires a new directory.")
        os.rename(temporary, target)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


class MarketSnapshot:
    """Verified immutable native market data; every query has DuckDB resource bounds."""

    def __init__(self, directory: Path | str):
        self.path = Path(directory).absolute()
        try:
            root_identity = _identity(self.path, True)
            manifest_path = self.path / "manifest.json"
            identity = _identity(manifest_path)
            if identity[2] > MAX_MANIFEST_BYTES:
                raise ValueError("Market snapshot manifest exceeds byte budget.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
                    or manifest.get("schema") != "market-snapshot-manifest.v1"
                    or manifest.get("all_vintages") is not True
                    or type(manifest.get("rows")) is not int or not 0 <= manifest["rows"] <= MAX_SNAPSHOT_ROWS
                    or not isinstance(manifest.get("tables"), dict) or set(manifest["tables"]) != set(TABLES)
                    or not isinstance(manifest.get("files"), list) or not 1 <= len(manifest["files"]) <= MAX_FILES):
                raise ValueError("Unsupported market snapshot manifest.")
            expected_hash = manifest.pop("content_sha256", None)
            if content_hash(manifest) != expected_hash:
                raise ValueError("Market snapshot manifest checksum mismatch.")
            manifest["content_sha256"] = expected_hash
            self.manifest = manifest
            verified_identities = [root_identity, identity]
            seen, total = set(), 0
            deadline = time.monotonic() + VERIFICATION_SECONDS
            for part in manifest["files"]:
                table, name = part.get("table"), part.get("file")
                if (table not in TABLES or not isinstance(name, str) or name in seen
                        or not name.startswith(table + "-") or not name.endswith(".parquet")
                        or "/" in name or "\\" in name
                        or type(part.get("rows")) is not int or not 0 <= part["rows"] <= 100_000
                        or type(part.get("bytes")) is not int or not 0 <= part["bytes"] <= MAX_FILE_BYTES):
                    raise ValueError("Unsafe market snapshot part manifest.")
                seen.add(name)
                total += part["rows"]
                path = self.path / name
                part_identity = _identity(path)
                if part_identity[2] != part["bytes"] or _file_hash(path, deadline) != part.get("sha256"):
                    raise ValueError("Market snapshot file checksum mismatch.")
                verified_identities.append(part_identity)
            if total != manifest["rows"] or {part["table"] for part in manifest["files"]} != set(TABLES):
                raise ValueError("Market snapshot row/table manifest mismatch.")
            self._fingerprint = self._identities()
            if self._fingerprint != tuple(verified_identities):
                raise ValueError("Market snapshot changed during checksum verification.")
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Market snapshot is missing or unreadable: {exc}") from exc

    def _identities(self):
        return (_identity(self.path, True), _identity(self.path / "manifest.json"),
                *(_identity(self.path / part["file"]) for part in self.manifest["files"]))

    def _views(self, con):
        if self._identities() != self._fingerprint:
            raise ValueError("Immutable market snapshot changed; reopen and verify it.")
        for table in TABLES:
            files = [str(self.path / part["file"]) for part in self.manifest["files"] if part["table"] == table]
            con.read_parquet(files).create_view(table)

    def _assert_unchanged(self):
        if self._identities() != self._fingerprint:
            raise ValueError("Immutable market snapshot changed during query.")

    def registry(self, as_of: int) -> dict | None:
        _epoch(as_of, "as_of")
        with analytical_query_connection() as con:
            self._views(con)
            if not con.execute("SELECT 1 FROM admissions WHERE kind LIKE 'registry:%' AND known_at<=? LIMIT 1", [as_of]).fetchone():
                return None
            cursor = con.execute("SELECT kind,payload FROM registry_components c WHERE kind<>'listing_identity' AND EXISTS "
                                 "(SELECT 1 FROM admissions a WHERE a.kind='registry:'||c.kind AND a.identity=c.identity AND a.known_at<=?) "
                                 "ORDER BY kind,identity LIMIT 150001", [as_of])

            def components():
                while rows := cursor.fetchmany(1000):
                    yield from rows

            result = _registry_components(components())
            self._assert_unchanged()
            return result

    def list_series(self, *, as_of: int, data_mode: str, limit: int = 100, cursor: str | None = None) -> dict:
        _epoch(as_of, "as_of")
        _mode(data_mode)
        _limit(limit, 1000, "limit")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
            raise ValueError("cursor must be a bounded series ID.")
        with analytical_query_connection() as con:
            self._views(con)
            rows = con.execute("SELECT id,payload FROM series s WHERE data_mode=? AND EXISTS "
                               "(SELECT 1 FROM series_imports i WHERE i.series_id=s.id AND i.known_at<=?) AND id>? ORDER BY id LIMIT ?", [data_mode, as_of, cursor or "", limit + 1]).fetchall()
            result = _bounded({"schema": "market-series-list.v1", "series": [json.loads(row[1]) for row in rows[:limit]],
                               "next_cursor": rows[limit - 1][0] if len(rows) > limit else None})
            self._assert_unchanged()
            return result

    def calendar(self, calendar_id: str, version: str | None = None, *, as_of: int) -> dict | None:
        _epoch(as_of, "as_of")
        if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 256:
            raise ValueError("calendar_id must be a bounded nonempty string.")
        if version is not None and (not isinstance(version, str) or not version or len(version) > 256):
            raise ValueError("version must be a bounded nonempty string.")
        with analytical_query_connection() as con:
            self._views(con)
            conditions, values = ["id=?", "EXISTS (SELECT 1 FROM admissions a WHERE a.kind='calendar' "
                                   "AND a.identity=CAST(json_array(c.id,c.version) AS VARCHAR) AND a.known_at<=?)"], [calendar_id, as_of]
            if version is not None:
                conditions.append("version=?")
                values.append(version)
            row = con.execute("SELECT payload FROM calendars c WHERE " + " AND ".join(conditions) + " ORDER BY declared_at DESC,version DESC LIMIT 1", values).fetchone()
            self._assert_unchanged()
            return _bounded(json.loads(row[0])) if row else None

    def fleet_summary(self, series_ids: list[str], *, as_of: int, data_mode: str, start: int,
                      end: int, max_rows: int = 1_000_000) -> dict:
        """Summarize native series in one bounded SQL query, with no coverage claim.

        Each immutable descriptor pins its class-independent native dimensions.
        No currencies, bases, calendars, listings or instruments are combined.
        Negative/zero prices retain absolute changes and disable simple returns.
        """
        _epoch(as_of, "as_of")
        _epoch(start, "start")
        _epoch(end, "end")
        _mode(data_mode)
        _limit(max_rows, MAX_SNAPSHOT_ROWS, "max_rows")
        if start >= end:
            raise ValueError("Fleet start must precede end.")
        if (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 1000
                or any(not isinstance(sid, str) or not sid or len(sid) > 256 for sid in series_ids)):
            raise ValueError("Fleet requires 1..1000 bounded native series IDs.")
        identifiers = sorted(set(series_ids))
        query = """WITH eligible AS (
          SELECT s.* FROM series s WHERE s.id IN (""" + ",".join("?" for _ in identifiers) + """)
          AND s.data_mode=? AND EXISTS (SELECT 1 FROM series_imports i WHERE i.series_id=s.id AND i.known_at<=?)
        ), latest AS (
          SELECT b.series_id,b.start,b."end",CAST(json_extract(b.payload,'$.close') AS DOUBLE) AS close
          FROM bars b INNER JOIN eligible s ON b.series_id=s.id
          WHERE b.known_at<=? AND b.start>=? AND b."end"<=? AND b."end"<=?
          QUALIFY ROW_NUMBER() OVER (PARTITION BY b.series_id,b.start,b."end" ORDER BY b.known_at DESC,b.observed_at DESC)=1
        ), summary AS (
          SELECT series_id,COUNT(*) AS rows,MIN(start) AS first_start,MAX("end") AS last_end,
                 arg_min(close,start) AS first_close,arg_max(close,start) AS last_close,MIN(close) AS minimum_close
          FROM latest GROUP BY series_id
        ) SELECT e.id,e.payload,COALESCE(m.rows,0),m.first_start,m.last_end,m.first_close,m.last_close,m.minimum_close
          FROM eligible e LEFT JOIN summary m ON e.id=m.series_id ORDER BY e.id"""
        with analytical_query_connection() as con:
            self._views(con)
            rows = con.execute(query, [*identifiers, data_mode, as_of, as_of, start, end, as_of]).fetchall()
            total = sum(row[2] for row in rows)
            if total > max_rows:
                raise ValueError("Fleet exceeds selected native row budget; narrow the interval.")
            series = []
            for sid, payload, count, first_start, last_end, first_close, last_close, minimum in rows:
                positive = count > 0 and minimum > 0
                series.append({"series_id": sid, "descriptor": json.loads(payload), "rows": count,
                               "first_start": first_start, "last_end": last_end,
                               "first_close": first_close, "last_close": last_close,
                               "absolute_change": last_close - first_close if count else None,
                               "simple_return": last_close / first_close - 1 if positive else None,
                               "simple_return_reason": None if positive else "no_observations" if count == 0 else "nonpositive_native_prices"})
            self._assert_unchanged()
            return _bounded({"schema": "market-fleet-summary.v1", "as_of": as_of, "data_mode": data_mode,
                             "start": start, "end": end, "total_rows": total, "series": series,
                             "missing_series_ids": sorted(set(identifiers) - {row[0] for row in rows}),
                             "coverage_note": "Observed native intervals only; calendar coverage and live acceptance are separate evidence."})

    def read_series(self, series_id: str, *, as_of: int, data_mode: str, start: int | None = None,
                    end: int | None = None, max_rows: int = 10_000) -> dict | None:
        _epoch(as_of, "as_of")
        _mode(data_mode)
        _limit(max_rows, MAX_READ_ROWS, "max_rows")
        if not isinstance(series_id, str) or not series_id or len(series_id) > 256:
            raise ValueError("series_id must be a bounded nonempty string.")
        for name, value in (("start", start), ("end", end)):
            if value is not None:
                _epoch(value, name)
        if start is not None and end is not None and start >= end:
            raise ValueError("start must precede end.")
        with analytical_query_connection() as con:
            self._views(con)
            row = con.execute("SELECT payload,hash,registry_hash FROM series s WHERE id=? AND data_mode=? AND EXISTS "
                              "(SELECT 1 FROM series_imports i WHERE i.series_id=s.id AND i.known_at<=?)", [series_id, data_mode, as_of]).fetchone()
            if row is None:
                return None
            conditions, values = ["series_id=?", "known_at<=?"], [series_id, as_of]
            if start is not None:
                conditions.append("start>=?")
                values.append(start)
            if end is not None:
                conditions.append('"end"<=?')
                values.append(end)
            rows = con.execute('SELECT payload,hash,observed_at,received_at,known_at FROM bars WHERE ' + " AND ".join(conditions) +
                               ' QUALIFY ROW_NUMBER() OVER (PARTITION BY start,"end" ORDER BY known_at DESC,observed_at DESC)=1 ORDER BY start,"end" LIMIT ?', values + [max_rows + 1]).fetchall()
            actions = con.execute("SELECT payload,hash,observed_at,received_at,known_at FROM actions WHERE series_id=? AND known_at<=? "
                                  "QUALIFY ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY known_at DESC,observed_at DESC)=1 ORDER BY effective_at,action_id LIMIT ?", [series_id, as_of, max_rows + 1]).fetchall()
            if len(rows) + len(actions) > max_rows:
                raise ValueError("Market snapshot exceeds result row budget; narrow the interval.")
            def receipt(item):
                return {"hash": item[1], "declared_observed_at": item[2], "received_at": item[3], "effective_known_at": item[4]}
            result = {"schema": "market-series.v1", "descriptor": json.loads(row[0]),
                      "bars": [json.loads(item[0]) for item in rows], "actions": [json.loads(item[0]) for item in actions],
                      "storage_provenance": {"descriptor_hash": row[1], "registry_hash": row[2],
                                             "receipt_evidence": "locally_witnessed" if data_mode == "live" else "self_declared_history",
                                             "bars": [receipt(item) for item in rows], "actions": [receipt(item) for item in actions]}}
            self._assert_unchanged()
            return _bounded(result)
