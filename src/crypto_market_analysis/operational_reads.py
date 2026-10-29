"""Bounded operational inspection of the configured store, without initialization.

No HistoryDataset or Store constructor is used here: both can mutate a store.
Missing datasets remain missing, and pending recovery journals remain untouched.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import StrictInt

from .reporting import bounded_as_of
from .storage import _LOCAL_RECORD_VERSION_ORDER


@contextmanager
def _read(database):
    path = Path(database).resolve()
    if not path.is_file():
        yield None
        return
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    finally:
        con.close()


def _tables(con):
    return set() if con is None else {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _source(source):
    if source is not None and (not isinstance(source, str) or not 1 <= len(source) <= 100):
        raise ValueError("source must contain 1–100 characters.")


def _page(con, table, selection, as_of, limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100.")
    scope = hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()
    if cursor is None:
        cutoff = int(time.time()) if as_of is None else bounded_as_of(as_of)
        high = con.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0] if con else 0
        state = {"scope": scope, "as_of": cutoff, "high": high, "offset": 0}
    else:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError
            state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (set(state) != {"scope", "as_of", "high", "offset"} or state["scope"] != scope
                    or any(type(state[k]) is not int or state[k] < 0 for k in ("as_of", "high", "offset"))
                    or (as_of is not None and bounded_as_of(as_of) != state["as_of"])):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid cursor or changed selection.") from exc
    return state


def _result(state, rows, limit, selection):
    more = len(rows) > limit
    rows = rows[:limit]
    next_state = {**state, "offset": state["offset"] + len(rows)}
    cursor = base64.urlsafe_b64encode(json.dumps(next_state, separators=(",", ":")).encode()).decode() if more else None
    return {"as_of": state["as_of"], "selection": selection, "rows": rows,
            "pagination": {"limit": limit, "returned": len(rows), "next_cursor": cursor,
                           "high_water_rowid": state["high"]}, "read_only": True}


def dataset_status(database, mode, *, section="summary", source=None, as_of=None, limit=20, cursor=None):
    """Inspect only the configured dataset. Mutable repair state has no known-at clock."""
    _source(source)
    if section not in ("summary", "captures", "conflicts", "attempts", "repairs"):
        raise ValueError("Unsupported dataset section.")
    if mode not in ("live", "import", "demo"):
        raise ValueError("Unsupported data mode.")
    marker = Path(database).parent / "dataset.json"
    if not marker.is_file():
        return {"status": "not_configured", "data_mode": mode, "read_only": True}
    if marker.stat().st_size > 2_000_000:
        raise ValueError("Dataset identity exceeds the 2 MB bound.")
    from .history_dataset import DatasetConfig

    config = DatasetConfig(**json.loads(marker.read_text()))
    if Path(config.directory).resolve() != Path(database).parent.resolve():
        raise ValueError("Dataset directory does not match configured store.")
    identity = {key: getattr(config, key) for key in ("name", "source", "data_mode", "target_start", "target_end")}
    base = {"dataset": identity, "read_only": True, "qualification": "unverified",
            "pending_recovery": "not_inspected_or_replayed"}
    if mode != config.data_mode or (source is not None and source != config.source):
        return {**base, "status": "selection_mismatch", "rows": []}
    with _read(database) as con:
        tables = _tables(con)
        if "history_captures" not in tables:
            return {**base, "status": "database_unavailable"}
        table = {"captures": "history_captures", "conflicts": "history_captures",
                 "attempts": "history_attempts", "repairs": "history_repairs", "summary": "history_captures"}[section]
        selection = {"kind": "dataset", "section": section, "mode": mode, "source": source}
        state = _page(con, table, selection, as_of, limit, cursor)
        cutoff, high = state["as_of"], state["high"]
        if section == "summary":
            counts = {"captures": con.execute("SELECT COUNT(*) FROM history_captures WHERE materialized_at<=? AND rowid<=?", (cutoff, high)).fetchone()[0]}
            version = hashlib.sha256()
            for row in con.execute("SELECT id FROM history_captures WHERE materialized_at<=? AND rowid<=? ORDER BY rowid", (cutoff, high)):
                version.update((row[0] + "\n").encode())
            markets = [dict(r) for r in con.execute("""SELECT asset,quote,COUNT(DISTINCT open_time) hours,
                MIN(open_time) first_open_time,MAX(close_time) last_close_time,MAX(observed_at) latest_receipt
                FROM candles WHERE source=? AND data_mode=? AND observed_at<=? AND close_time<=?
                AND open_time>=? AND open_time<? GROUP BY asset,quote ORDER BY asset,quote""",
                (config.source, mode, cutoff, cutoff, config.target_start, config.target_end))]
            return {**base, "status": "available", "as_of": cutoff, "counts": counts, "markets": markets,
                    "dataset_version": "sha256:" + version.hexdigest(), "version_basis": "ordered known-at immutable capture IDs",
                    "target_hours_per_market": (config.target_end - config.target_start) // 3600,
                    "coverage_basis": "distinct known-at closed hourly records within target; missing markets remain unknown"}
        if section == "repairs":
            if as_of is not None or cursor is not None:
                raise ValueError("Repair state is mutable without version timestamps; historical/cursor reads are unavailable.")
            rows = [json.loads(r[0]) for r in con.execute("SELECT payload FROM history_repairs ORDER BY market LIMIT ?", (limit + 1,))]
            return {**base, "status": "available", "rows": rows[:limit], "truncated": len(rows) > limit,
                    "time_basis": "current_unversioned_state; unavailable_at_historical_cutoff"}
        if section == "captures":
            sql = """SELECT json_remove(payload,'$.conflicts') payload,
                COALESCE(json_array_length(payload,'$.conflicts'),0) conflict_count FROM history_captures
                WHERE materialized_at<=? AND rowid<=? ORDER BY rowid LIMIT ? OFFSET ?"""
            rows = [{**json.loads(r[0]), "conflicts_total": r[1]} for r in con.execute(sql, (cutoff, high, limit + 1, state["offset"]))]
        elif section == "conflicts":
            rows = [{"capture_id": r[0], "conflict": json.loads(r[1])} for r in con.execute("""
                SELECT c.id,j.value FROM history_captures c,json_each(c.payload,'$.conflicts') j
                WHERE c.materialized_at<=? AND c.rowid<=? ORDER BY c.rowid,CAST(j.key AS INTEGER) LIMIT ? OFFSET ?""",
                (cutoff, high, limit + 1, state["offset"]))]
        else:
            rows = [{"asset": r[0], "quote": r[1], **json.loads(r[2])} for r in con.execute("""
                SELECT asset,quote,payload FROM history_attempts WHERE json_extract(payload,'$.attempted_at')<=?
                AND rowid<=? ORDER BY rowid LIMIT ? OFFSET ?""", (cutoff, high, limit + 1, state["offset"]))]
        return {**base, "status": "available", **_result(state, rows, limit, selection)}


def collection_history(database, mode, *, source=None, as_of=None, limit=20, cursor=None, section="runs"):
    _source(source)
    if section not in ("runs", "unclassified_runs", "state"):
        raise ValueError("Unsupported collection section.")
    selection = {"kind": "runs", "section": section, "data_mode": mode, "source": source}
    with _read(database) as con:
        if section == "state":
            if as_of is not None or cursor is not None:
                raise ValueError("Scheduler state is unversioned; historical/cursor reads are unavailable.")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("limit must be an integer from 1 to 100.")
            available = "scheduler_state" in _tables(con)
            rows = [dict(r) for r in con.execute("SELECT * FROM scheduler_state WHERE (? IS NULL OR source=?) ORDER BY source,task LIMIT ?",
                    (source, source, limit + 1))] if available else []
            return {"selection": selection, "rows": rows[:limit], "truncated": len(rows) > limit,
                    "status": "available" if available else "not_recorded", "read_only": True,
                    "time_basis": "current_unversioned_scheduler_state", "mode_basis": "scheduler source/task state has no market mode label"}
        available = "runs" in _tables(con)
        state = _page(con if available else None, "runs", selection, as_of, limit, cursor)
        rows = []
        if available:
            mode_filter = "json_extract(payload,'$.data_mode')=?" if section == "runs" else "json_extract(payload,'$.data_mode') IS NULL"
            parameters = [state["as_of"], state["high"], source, source]
            if section == "runs":
                parameters.append(mode)
            rows = [{"run_id": r[0], **json.loads(r[1])} for r in con.execute(f"""SELECT id,payload FROM runs
                WHERE observed_at<=? AND id<=? AND (? IS NULL OR source=?)
                AND {mode_filter} ORDER BY id LIMIT ? OFFSET ?""", [*parameters, limit + 1, state["offset"]])]
            unknown = con.execute("""SELECT COUNT(*) FROM runs WHERE observed_at<=? AND id<=?
                AND (? IS NULL OR source=?) AND json_extract(payload,'$.data_mode') IS NULL""",
                (state["as_of"], state["high"], source, source)).fetchone()[0]
        else:
            unknown = 0
        return {**_result(state, rows, limit, selection), "status": "available" if available else "database_unavailable",
                "excluded_mode_unknown_runs": unknown,
                "counter_basis": "persisted run payload only; HTTP request count unavailable unless explicitly recorded"}


def upcoming_calendar(database, mode, *, source=None, as_of=None, start=None, end=None, limit=20, cursor=None):
    _source(source)
    # Date-only schedules use their civil date for filtering, never a fabricated event clock.
    selection = {"kind": "calendar", "data_mode": mode, "source": source, "start": start, "end": end}
    with _read(database) as con:
        available = "local_records" in _tables(con)
        state = _page(con if available else None, "local_records", selection, as_of, limit, cursor)
        lower = state["as_of"] if start is None else bounded_as_of(start)
        upper = lower + 30 * 86400 if end is None else bounded_as_of(end)
        if not lower <= upper <= lower + 366 * 86400:
            raise ValueError("Calendar range must span at most 366 days.")
        rows = []
        if available:
            # Rank every known revision before source/range/status filtering so a
            # moved or cancelled event cannot resurrect an earlier version.
            sql = f"""WITH versions AS (SELECT id,payload,ROW_NUMBER() OVER(PARTITION BY identity
                ORDER BY {_LOCAL_RECORD_VERSION_ORDER}) rn FROM local_records
                WHERE kind='calendar' AND observed_at<=? AND id<=?), latest AS
                (SELECT id,payload FROM versions WHERE rn=1)
                SELECT payload FROM latest WHERE (? IS NULL OR json_extract(payload,'$.source')=?)
                AND (json_extract(payload,'$.data_mode')=? OR json_extract(payload,'$.data_mode') IS NULL)
                AND COALESCE(json_extract(payload,'$.status'),'planned') NOT IN ('cancelled','canceled')
                AND (json_extract(payload,'$.event_time') BETWEEN ? AND ? OR
                    (json_extract(payload,'$.event_time') IS NULL AND
                     COALESCE(json_extract(payload,'$.local_date'),json_extract(payload,'$.date')) BETWEEN date(?,'unixepoch') AND date(?,'unixepoch')))
                ORDER BY COALESCE(json_extract(payload,'$.event_time'),
                    unixepoch(COALESCE(json_extract(payload,'$.local_date'),json_extract(payload,'$.date')))),id
                LIMIT ? OFFSET ?"""
            rows = [json.loads(r[0]) for r in con.execute(sql, (state["as_of"], state["high"], source, source,
                    mode, lower, upper, lower, upper, limit + 1, state["offset"]))]
        return {**_result(state, rows, limit, selection), "range": {"start": lower, "end": upper},
                "calendar_mode_basis": "explicit matching modes plus unlabelled source schedules; never market observations",
                "time_basis": "verified event clocks or inclusive source civil dates; schedule is not publication"}


def storage_status(database, mode, *, source=None, as_of=None, policy=None):
    from .storage_policy import StoragePolicy, physical_storage

    _source(source)
    cutoff = int(time.time()) if as_of is None else bounded_as_of(as_of)
    physical = physical_storage(database)
    counts = {}
    with _read(database) as con:
        tables = _tables(con)
        for table in ("candles", "observations"):
            if table in tables:
                counts[table] = dict(con.execute(f"""SELECT COUNT(*) stored_versions,MIN(observed_at) oldest_receipt,
                    MAX(observed_at) newest_receipt FROM {table} WHERE data_mode=? AND observed_at<=?
                    AND (? IS NULL OR source=?)""", (mode, cutoff, source, source)).fetchone())
    return {"as_of": cutoff, "selection": {"data_mode": mode, "source": source}, "families": counts,
            "physical": physical, "physical_scope": "current whole configured database; not historical or source-specific",
            "retention": {"policy": asdict(policy or StoragePolicy()), "policy_basis": "configured" if policy else "defaults",
                          "archive_reads": "explicit_only", "scope": "current bytes are not historical/source-specific",
                          "protected": ["candles", "raw_derivatives", "source_provenance", "assessment_journal"],
                          "limitations": "Archived coverage requires explicit archive reads. Quotas cannot bound a single SQLite WAL transaction."},
            "read_only": True}


def register_operational_tools(server, engine, readonly, present=None):
    register = present or server.tool(annotations=readonly)

    @register
    def get_request_schema(tool_name: str) -> dict:
        """Discover a bounded JSON request schema, required fields, defaults and a runnable synthetic example. Use 'list' for names."""
        from .request_schemas import get_request_schema as lookup
        return lookup(tool_name)

    @register
    def get_dataset_status(section: Literal["summary", "captures", "conflicts", "attempts", "repairs"] = "summary",
                           source: str | None = None, as_of: StrictInt | None = None,
                           limit: StrictInt = 20, cursor: str | None = None) -> dict:
        """Inspect the configured import dataset only, with bounded capture/conflict/attempt pages. No initialization, pending recovery or collection. Repair state is current and unversioned."""
        return dataset_status(engine.settings.database, engine.mode, section=section, source=source, as_of=as_of, limit=limit, cursor=cursor)

    @register
    def get_collection_history(source: str | None = None, as_of: StrictInt | None = None,
                               limit: StrictInt = 20, cursor: str | None = None,
                               section: Literal["runs", "unclassified_runs", "state"] = "runs") -> dict:
        """Read persisted configured-mode runs at a frozen cutoff. Select unclassified_runs separately for legacy runs with unknown mode, or state for current unversioned scheduler cadence/backoff."""
        return collection_history(engine.settings.database, engine.mode, source=source, as_of=as_of, limit=limit, cursor=cursor, section=section)

    @register
    def get_storage_status(source: str | None = None, as_of: StrictInt | None = None) -> dict:
        """Read known-at source/mode storage counts and current physical bytes. Physical sizes cover the whole configured store and are never historical estimates."""
        from .storage_policy import StoragePolicy
        return storage_status(engine.settings.database, engine.mode, source=source, as_of=as_of,
                              policy=StoragePolicy.from_settings(engine.settings))

    @register
    def get_upcoming_calendar(source: str | None = None, as_of: StrictInt | None = None,
                              start: StrictInt | None = None, end: StrictInt | None = None,
                              limit: StrictInt = 20, cursor: str | None = None) -> dict:
        """Read bounded upcoming sourced schedules known at as_of; default range 30 days, maximum 366. Civil dates stay date-only; cancelled revisions supersede prior plans."""
        return upcoming_calendar(engine.settings.database, engine.mode, source=source, as_of=as_of,
                                 start=start, end=end, limit=limit, cursor=cursor)
