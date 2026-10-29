"""Versioned SQLite WAL store: a collector can write while MCP reads snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .evidence import metric_dimension
from .metric_history import decode_cursor, encode_cursor
from .metric_history import selection as history_selection
from .models import Candle, CollectionResult, Observation

# A later receipt of an older manual ECB revision must not undo a known
# cancellation. Apply the same known-at version order to both calendar readers.
_LOCAL_RECORD_VERSION_ORDER = """CASE WHEN kind='calendar'
    AND json_extract(payload,'$.source_format')='ecb-manual-calendar-v1'
    AND json_type(payload,'$.sequence')='integer'
    AND json_extract(payload,'$.sequence') BETWEEN 0 AND 2147483647
    THEN json_extract(payload,'$.sequence') ELSE 0 END DESC,observed_at DESC,id DESC"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _hash(value: dict) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class Store:
    """Public boundary for idempotent, source-specific, known-at data access."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, asset TEXT NOT NULL,
                    quote TEXT NOT NULL, timeframe TEXT NOT NULL, open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL, observed_at INTEGER NOT NULL, data_mode TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS candle_lookup ON candles
                    (source,asset,quote,timeframe,open_time,data_mode,observed_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, asset TEXT NOT NULL,
                    metric TEXT NOT NULL, event_time INTEGER NOT NULL, observed_at INTEGER NOT NULL,
                    data_mode TEXT NOT NULL, dimension TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observation_lookup ON observations
                    (source,asset,metric,event_time,data_mode,dimension,observed_at DESC,id DESC);
                CREATE INDEX IF NOT EXISTS observation_history ON observations
                    (asset,metric,data_mode,event_time DESC,id DESC);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, task TEXT NOT NULL,
                    observed_at INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_schedule_lookup ON runs
                    (source,task,observed_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT NOT NULL, observed_at INTEGER NOT NULL, event_time INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(id,observed_at)
                );
                CREATE TABLE IF NOT EXISTS local_records (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL,
                    observed_at INTEGER NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(kind,fingerprint)
                );
            """)
            # Add dimensions without dropping observations or rewriting their source payloads.
            if con.execute("PRAGMA user_version").fetchone()[0] < 2:
                for row in con.execute("SELECT id,payload FROM observations").fetchall():
                    dimension = _json(metric_dimension(Observation(**json.loads(row["payload"]))))
                    con.execute("UPDATE observations SET dimension=? WHERE id=?", (dimension, row["id"]))
                con.execute("PRAGMA user_version=2")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=15000")
            with con:
                yield con
        finally:
            con.close()

    def put_candles(self, rows: list[Candle]) -> int:
        inserted = 0
        with self.connection() as con:
            for c in rows:
                if c.asset not in ("BTC", "ETH") or c.data_mode not in ("live", "demo", "import"):
                    raise ValueError("Unsupported asset or data mode.")
                numeric = (c.open, c.high, c.low, c.close, c.volume)
                if any(type(getattr(c, key)) is not int for key in ("open_time", "close_time", "observed_at")):
                    raise ValueError("Candle timestamps must be integer UTC epoch seconds.")
                if any(isinstance(v, bool) for v in numeric) or not all(math.isfinite(v) for v in numeric):
                    raise ValueError("Candle values must be finite numbers.")
                if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
                    raise ValueError("Candle prices must be positive and volume nonnegative.")
                if c.high < max(c.open, c.low, c.close) or c.low > min(c.open, c.high, c.close):
                    raise ValueError("Inconsistent candle high or low.")
                if c.timeframe != "1h" or c.open_time % 3600 or c.close_time != c.open_time + 3600:
                    raise ValueError("Expected an aligned one-hour UTC candle.")
                if c.close_time > c.observed_at:
                    raise ValueError("An open candle cannot be stored.")
                if c.quote_volume is not None and (not math.isfinite(c.quote_volume) or c.quote_volume < 0):
                    raise ValueError("Invalid quote volume.")
                if c.taker_buy_quote_volume is not None and (
                    not math.isfinite(c.taker_buy_quote_volume)
                    or c.taker_buy_quote_volume < 0
                    or c.quote_volume is None
                    or c.taker_buy_quote_volume > c.quote_volume * 1.000001
                ):
                    raise ValueError("Inconsistent taker buy volume.")
                payload = asdict(c)
                fingerprint = _hash({k: v for k, v in payload.items() if k != "observed_at"})
                key = (c.source, c.asset, c.quote, c.timeframe, c.open_time, c.data_mode)
                latest = con.execute(
                    """SELECT fingerprint,observed_at FROM candles WHERE
                    source=? AND asset=? AND quote=? AND timeframe=? AND open_time=? AND data_mode=?
                    ORDER BY observed_at DESC,id DESC LIMIT 1""",
                    key,
                ).fetchone()
                if latest and latest["fingerprint"] == fingerprint:
                    continue
                con.execute(
                    """INSERT INTO candles(source,asset,quote,timeframe,open_time,close_time,
                    observed_at,data_mode,fingerprint,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (*key[:5], c.close_time, c.observed_at, c.data_mode, fingerprint, _json(payload)),
                )
                inserted += 1
        return inserted

    def put_observations(self, rows: list[Observation]) -> int:
        inserted = 0
        with self.connection() as con:
            for o in rows:
                if (isinstance(o.value, bool) or not math.isfinite(o.value)
                    or type(o.event_time) is not int or type(o.observed_at) is not int
                    or o.event_time > o.observed_at):
                    raise ValueError("Non-finite observation or observation dated in the future.")
                if o.data_mode not in ("live", "demo", "import"):
                    raise ValueError("Invalid data mode.")
                payload = asdict(o)
                fingerprint = _hash({k: v for k, v in payload.items() if k != "observed_at"})
                dimension = _json(metric_dimension(o))
                key = (o.source, o.asset, o.metric, o.event_time, o.data_mode, dimension)
                latest = con.execute(
                    """SELECT fingerprint FROM observations WHERE source=? AND asset=?
                    AND metric=? AND event_time=? AND data_mode=? AND dimension=?
                    ORDER BY observed_at DESC,id DESC LIMIT 1""",
                    key,
                ).fetchone()
                if latest and latest[0] == fingerprint:
                    continue
                con.execute(
                    """INSERT INTO observations(source,asset,metric,event_time,observed_at,
                    data_mode,dimension,fingerprint,payload) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        o.source,
                        o.asset,
                        o.metric,
                        o.event_time,
                        o.observed_at,
                        o.data_mode,
                        dimension,
                        fingerprint,
                        _json(payload),
                    ),
                )
                inserted += 1
        return inserted

    def record_run(self, result: CollectionResult, task: str, now: int) -> dict:
        payload = {
            "source": result.source,
            "task": task,
            "observed_at": now,
            "candles": len(result.candles),
            "observations": len(result.observations),
            "warnings": result.warnings,
            "errors": result.errors,
            "status": "partial"
            if result.errors and (result.candles or result.observations)
            else "error"
            if result.errors
            else "ok"
            if result.candles or result.observations
            else "empty",
        }
        with self.connection() as con:
            con.execute(
                "INSERT INTO runs(source,task,observed_at,payload) VALUES(?,?,?,?)",
                (result.source, task, now, _json(payload)),
            )
        return payload

    def save_result(self, result: CollectionResult, task: str, now: int) -> dict:
        candles = observations = 0
        try:
            candles = self.put_candles(result.candles)
        except (ValueError, TypeError) as exc:
            result.errors.append(f"Candle validation: {exc}")
            result.candles = []
        try:
            observations = self.put_observations(result.observations)
        except (ValueError, TypeError) as exc:
            result.errors.append(f"Observation validation: {exc}")
            result.observations = []
        return {
            **self.record_run(result, task, now),
            "inserted_candles": candles,
            "inserted_observations": observations,
        }

    def candles(
        self,
        asset: str,
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        mode: str = "live",
        max_id: int | None = None,
        versions: bool = False,
    ) -> list[Candle]:
        cutoff = int(time.time()) if as_of is None else as_of
        conditions, values = (
            ["asset=?", "data_mode=?", "close_time<=?", "observed_at<=?"],
            [asset, mode, cutoff, cutoff],
        )
        for name, value in (("source", source), ("quote", quote)):
            if value is not None:
                conditions.append(f"{name}=?")
                values.append(value)
        if max_id is not None:
            conditions.append("id<=?")
            values.append(max_id)
        with self.connection() as con:
            if versions:
                rows = con.execute(
                    f"SELECT payload FROM candles WHERE {' AND '.join(conditions)} ORDER BY open_time,observed_at,id",
                    values,
                ).fetchall()
            else:
                rows = con.execute(
                    f"""SELECT payload FROM (SELECT payload,open_time,
                ROW_NUMBER() OVER(PARTITION BY source,asset,quote,timeframe,open_time,data_mode
                ORDER BY observed_at DESC,id DESC) rn FROM candles WHERE {" AND ".join(conditions)})
                WHERE rn=1 ORDER BY open_time""",
                    values,
                ).fetchall()
        return [Candle(**json.loads(row[0])) for row in rows]

    @staticmethod
    def _metric_history_query(selected: dict, cutoff: int, watermark: int):
        lower = selected["start"] if selected["start"] is not None else cutoff - 365 * 86400
        upper = selected["end"] if selected["end"] is not None else cutoff
        if lower > upper or upper > cutoff:
            raise ValueError("History bounds must satisfy start <= end <= as_of.")
        conditions = ["asset=?", "data_mode=?", "event_time>=?", "event_time<=?",
                      "observed_at<=?", "id<=?"]
        values = [selected["asset"], selected["mode"], lower, upper, cutoff, watermark]
        for key in ("metric", "source"):
            if selected[key] is not None:
                conditions.append(f"{key}=?")
                values.append(selected[key])
        for key in ("quote", "unit"):
            if selected[key] is not None:
                conditions.append(f"json_extract(dimension,'$.{key}')=?")
                values.append(selected[key])
        if selected["dimension"] is not None:
            conditions.append("dimension=?")
            values.append(_json(selected["dimension"]))
        query = f"""WITH ranked AS (
            SELECT id,payload,source,metric,dimension,event_time,
            ROW_NUMBER() OVER(PARTITION BY source,asset,metric,event_time,data_mode,dimension
                ORDER BY observed_at DESC,id DESC) rn
            FROM observations WHERE {' AND '.join(conditions)}),
            latest AS (SELECT * FROM ranked WHERE rn=1) """
        return query, values, lower, upper

    def metric_history(self, asset: str, metric: str, source: str | None = None,
                       limit: int = 100, *, mode: str = "live", quote: str | None = None,
                       unit: str | None = None, dimension: dict | None = None,
                       as_of: int | None = None, start: int | None = None,
                       end: int | None = None, cursor: str | None = None) -> dict:
        """Read selected known-at history, newest first, without a global observation cap.

        A cursor pins the receipt watermark and cutoff, not a durable evidence archive.
        Retention/deletion can invalidate it; detectable changes fail instead of silently
        continuing over gaps. Callers needing durable evidence must archive the result.
        """
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        if metric is None:
            raise ValueError("metric is required.")
        selected = history_selection(asset, metric, mode, source, quote, unit, dimension, as_of, start, end)
        token = decode_cursor(cursor) if cursor is not None else None
        database = hashlib.sha256(str(self.path).encode()).hexdigest()
        if token is not None and (token.get("selection") != selected or token.get("database") != database):
            raise ValueError("Metric history cursor does not match this selection or store.")
        with self.connection() as con:
            con.execute("BEGIN")
            if token is None:
                cutoff = int(time.time()) if as_of is None else as_of
                watermark = con.execute("SELECT COALESCE(MAX(id),0) FROM observations").fetchone()[0]
            else:
                cutoff, watermark = token.get("cutoff"), token.get("watermark")
                if (type(cutoff) is not int or cutoff < 0 or cutoff > int(time.time())
                        or type(watermark) is not int or watermark < 0):
                    raise ValueError("Invalid metric history cursor bounds.")
            query, values, lower, upper = self._metric_history_query(selected, cutoff, watermark)
            # Selected-row identities detect ordinary retention without counting the
            # entire database. A maximum-row fingerprint detects rowid reuse. These
            # checks are deliberately not a cryptographic frozen evidence archive.
            stamp = con.execute(query + "SELECT COUNT(*),COALESCE(SUM(id),0),MAX(id),"
                                "COUNT(DISTINCT json_array(source,dimension)) FROM latest",
                                values).fetchone()
            fence = con.execute("SELECT fingerprint FROM observations WHERE id=?", (stamp[2],)).fetchone()
            retention = [*stamp[:3], fence[0] if fence else None]
            if token is not None and token.get("retention") != retention:
                raise ValueError("Stale metric history cursor: stored observations changed or were retained away.")
            series_count = stamp[3]
            seek = ""
            after_values = []
            if token is not None:
                after = token.get("after")
                if (not isinstance(after, list) or len(after) != 2
                        or any(type(value) is not int for value in after)
                        or not lower <= after[0] <= upper or not 1 <= after[1] <= watermark):
                    raise ValueError("Invalid metric history cursor position.")
                seek = "WHERE event_time<? OR (event_time=? AND id<?)"
                after_values = [after[0], after[0], after[1]]
            rows = con.execute(query + f"""SELECT *,COUNT(*) OVER() AS remaining FROM latest {seek}
                               ORDER BY event_time DESC,id DESC LIMIT ?""",
                               [*values, *after_values, limit + 1]).fetchall()
        available = rows[0]["remaining"] if rows else 0
        candidates = rows[:limit]
        points = [json.loads(row["payload"]) for row in candidates]

        def page(count):
            retained = candidates[:count]
            remaining = available - count
            next_cursor = None
            if remaining and retained:
                next_cursor = encode_cursor({"version": 1, "database": database, "selection": selected,
                                             "cutoff": cutoff, "watermark": watermark, "retention": retention,
                                             "after": [retained[-1]["event_time"], retained[-1]["id"]]})
            series = {(row["source"], row["dimension"]) for row in retained}
            return {"asset": asset, "metric": metric, "data_mode": mode,
                    "points": points[:count], "truncated": bool(remaining),
                    "next_cursor": next_cursor, "remaining_points": remaining,
                    "requested_limit": limit, "returned_points": count,
                    "truncation_reasons": (["page_limit"] if available > limit else [])
                    + (["response_byte_budget"] if count < len(candidates) else []),
                    "response_byte_budget": 10000,
                    "series_count": series_count, "multiple_series": series_count > 1,
                    "series": [{"source": source, "dimension": json.loads(dimension)}
                               for source, dimension in sorted(series)],
                    "provenance": {"selection": selected, "as_of": cutoff, "start": lower, "end": upper,
                                   "observation_watermark": watermark, "order": "event_time DESC,id DESC",
                                   "version_policy": "latest known receipt per source/mode/native dimension",
                                   "cursor_retention": "Not archived; detectable deletion invalidates continuation."}}

        # Leave room for both JSON and text copies in the MCP response. Build the
        # continuation only after the emitted prefix is chosen, so byte bounding
        # cannot skip points. A single oversize point fails explicitly, not a loop.
        result = page(len(candidates))
        if len(_json(result).encode()) <= 10000:
            return result
        if not candidates or len(_json(page(1)).encode()) > 10000:
            raise ValueError("A selected history point or selector exceeds the 10000-byte response budget; "
                             "history cannot emit or advance past it. Use a local evidence export.")
        low, high = 1, len(candidates)
        while low < high:
            middle = (low + high + 1) // 2
            if len(_json(page(middle)).encode()) <= 10000:
                low = middle
            else:
                high = middle - 1
        return page(low)

    def metric_series(self, asset: str, metric: str | None = None, *, source: str | None = None,
                      mode: str = "live", quote: str | None = None, unit: str | None = None,
                      dimension: dict | None = None, as_of: int | None = None,
                      start: int | None = None, end: int | None = None, limit: int = 100) -> dict:
        """Discover bounded native series and their available known-at date coverage."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        selected = history_selection(asset, metric, mode, source, quote, unit, dimension, as_of, start, end)
        cutoff = int(time.time()) if as_of is None else as_of
        with self.connection() as con:
            con.execute("BEGIN")
            watermark = con.execute("SELECT COALESCE(MAX(id),0) FROM observations").fetchone()[0]
            query, values, lower, upper = self._metric_history_query(selected, cutoff, watermark)
            rows = con.execute(query + """SELECT source,metric,dimension,COUNT(*) AS points,
                MIN(event_time) AS start,MAX(event_time) AS end,COUNT(*) OVER() AS series_count
                FROM latest GROUP BY source,metric,dimension ORDER BY source,metric,dimension LIMIT ?""",
                               [*values, limit + 1]).fetchall()
        return {"asset": asset, "data_mode": mode, "as_of": cutoff, "start": lower, "end": upper,
                "series": [{"source": row["source"], "metric": row["metric"],
                            "dimension": json.loads(row["dimension"]), "points": row["points"],
                            "start": row["start"], "end": row["end"]} for row in rows[:limit]],
                "truncated": len(rows) > limit,
                "omitted_series": rows[0]["series_count"] - min(len(rows), limit) if rows else 0,
                "observation_watermark": watermark}

    def observations(
        self,
        asset: str | None = None,
        as_of: int | None = None,
        mode: str = "live",
        days: int = 365,
        limit: int = 200000,
        max_id: int | None = None,
    ) -> list[Observation]:
        cutoff = int(time.time()) if as_of is None else as_of
        conditions = ["data_mode=?", "event_time<=?", "observed_at<=?", "event_time>=?"]
        values: list = [mode, cutoff, cutoff, cutoff - days * 86400]
        if asset is not None:
            conditions.append("asset=?")
            values.append(asset)
        if max_id is not None:
            conditions.append("id<=?")
            values.append(max_id)
        values.append(max(1, min(limit, 500000)))
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT payload FROM (SELECT payload,event_time,
                ROW_NUMBER() OVER(PARTITION BY source,asset,metric,event_time,data_mode,dimension
                ORDER BY observed_at DESC,id DESC) rn FROM observations WHERE {" AND ".join(conditions)})
                WHERE rn=1 ORDER BY event_time DESC LIMIT ?""",
                values,
            ).fetchall()
        return [Observation(**json.loads(row[0])) for row in rows]

    def runs(self, as_of: int | None = None, limit: int = 100) -> list[dict]:
        cutoff = int(time.time()) if as_of is None else as_of
        with self.connection() as con:
            rows = con.execute(
                """SELECT payload FROM (SELECT payload,observed_at,
                ROW_NUMBER() OVER(PARTITION BY source,task ORDER BY observed_at DESC,id DESC) rn
                FROM runs WHERE observed_at<=?) WHERE rn=1 ORDER BY observed_at DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def scheduler_runs(self) -> list[dict]:
        """Latest run per source/task, without a global history limit.

        The grouped index scan and indexed last-row lookups keep run history in SQL.
        Only one payload per source/task crosses the storage boundary.
        """
        with self.connection() as con:
            rows = con.execute("""
                SELECT (SELECT payload FROM runs latest
                        WHERE latest.source=groups.source AND latest.task=groups.task
                        ORDER BY observed_at DESC,id DESC LIMIT 1), groups.last_success
                FROM (SELECT source,task,MAX(CASE WHEN json_array_length(payload,'$.errors')=0
                      THEN observed_at ELSE 0 END) last_success
                      FROM runs GROUP BY source,task) groups
            """).fetchall()
        return [{**json.loads(row[0]), "last_success": row[1]} for row in rows]

    def counts(self) -> dict:
        with self.connection() as con:
            return {
                name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("candles", "observations", "events", "runs")
            }

    def watermark(self) -> dict:
        with self.connection() as con:
            return {
                table: con.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]
                for table in ("candles", "observations", "events", "runs")
            }

    def _capture_observations(self, con, asset, as_of, mode, profile, source, quote,
                              limit, baseline_seconds):
        """Bound each native series before sharing the total capture budget.

        Bundle summaries consume the latest two events and the nearest preceding
        7/21-day comparisons. These rows have priority over optional recent history.
        Vigilance consumes every raw baseline event and two native derivative events;
        if those required rows do not fit, refuse the capture rather than bias a median.
        """
        families = ("liquidity", "derivatives", "options", "macro", "stablecoins", "other")
        if profile == "candles":
            return [], [{"family": family, "status": "not_requested", "captured": 0}
                        for family in families]
        conditions = ["asset IN (?, 'MACRO', 'STABLECOINS')", "data_mode=?",
                      "event_time<=?", "observed_at<=?", "event_time>=?"]
        values = [asset, mode, as_of, as_of, as_of - 730 * 86400]
        if profile == "vigilance":
            conditions.append("asset=?")
            values.append(asset)
            conditions.append("""((metric IN ('spread_bps','depth_bid_1pct','depth_ask_1pct')
                AND json_extract(dimension,'$.aggregation') IS NULL)
                OR metric LIKE 'funding_rate_%' OR metric LIKE 'open_interest_%')""")
            if quote is not None:
                conditions.append("json_extract(dimension,'$.quote')=?")
                values.append(quote)
            if source is not None:
                conditions.append("(source=? OR metric LIKE 'funding_rate_%' OR metric LIKE 'open_interest_%')")
                values.append(source)
        # Dimension is the persisted canonical source/quote/unit/instrument identity.
        identity = "source,asset,metric,data_mode,dimension"
        cte = f"""WITH versions AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY {identity},event_time
                ORDER BY observed_at DESC,id DESC) version_rank
            FROM observations WHERE {' AND '.join(conditions)}),
            latest AS (SELECT *, CASE
                WHEN asset='MACRO' THEN 'macro'
                WHEN asset='STABLECOINS' THEN 'stablecoins'
                WHEN metric LIKE 'option_%' THEN 'options'
                WHEN metric LIKE 'funding_rate_%' OR metric LIKE 'open_interest_%' THEN 'derivatives'
                WHEN metric LIKE 'depth_%' OR metric LIKE 'spread_%' OR metric LIKE 'book_%'
                     OR metric LIKE 'mid_%' THEN 'liquidity'
                ELSE 'other' END family FROM versions WHERE version_rank=1),
            ranked AS (SELECT *, ROW_NUMBER() OVER(PARTITION BY {identity}
                ORDER BY event_time DESC,id DESC) series_rank,
                MAX(event_time) OVER(PARTITION BY {identity}) latest_time FROM latest),
            comparisons AS (SELECT *,
                MAX(CASE WHEN event_time<=latest_time-604800 THEN event_time END)
                    OVER(PARTITION BY {identity}) comparison_7d,
                MAX(CASE WHEN event_time<=latest_time-1814400 THEN event_time END)
                    OVER(PARTITION BY {identity}) comparison_21d FROM ranked)
            """
        if profile == "vigilance":
            wanted = "(family='liquidity' AND event_time>=latest_time-?) OR (family='derivatives' AND series_rank<=2)"
            selection_values = [*values, baseline_seconds]
            required = "1"
        else:
            wanted = "series_rank<=256 OR event_time=comparison_7d OR event_time=comparison_21d"
            selection_values = values
            required = "COALESCE((series_rank<=2 OR event_time=comparison_7d OR event_time=comparison_21d),0)"
        available = con.execute(cte + f"""SELECT family,COUNT(*),
            COUNT(DISTINCT json_array(source,asset,metric,dimension)),MIN(event_time),MAX(event_time),
            SUM(CASE WHEN ({wanted}) AND ({required}) THEN 1 ELSE 0 END)
            FROM comparisons GROUP BY family""", selection_values).fetchall()
        rows = con.execute(cte + f""", candidates AS (
            SELECT *, {required} required FROM comparisons WHERE {wanted})
            SELECT payload,family,required,SUM(required) OVER () required_count
            FROM candidates ORDER BY required DESC,series_rank,source,asset,metric,dimension,event_time DESC
            LIMIT ?""", [*selection_values, limit + 1]).fetchall()
        if rows and rows[0][3] > limit:
            raise ValueError(f"Required {profile} observation evidence exceeds the {limit}-row capture limit.")
        selected = rows[:limit]
        observations = [Observation(**json.loads(row[0])) for row in selected]
        coverage = []
        for family, count, series, first, last, required_count in available:
            retained = [obs for obs, row in zip(observations, selected, strict=True) if row[1] == family]
            coverage.append({"family": family, "status": "captured", "available": count, "series": series,
                             "captured": len(retained), "omitted": count - len(retained),
                             "required_available": required_count,
                             "required_captured": sum(row[2] for row in selected if row[1] == family),
                             "required_truncated": False,
                             "truncated": count > len(retained), "available_start": first, "available_end": last,
                             "captured_start": min((obs.event_time for obs in retained), default=None),
                             "captured_end": max((obs.event_time for obs in retained), default=None),
                             "selection": "raw_baseline_and_latest_native_pair" if profile == "vigilance"
                             else "latest_256_per_series_plus_7d_21d_comparisons; required_rows_first"})
        coverage.extend({"family": family, "status": "missing", "available": 0, "captured": 0,
                         "omitted": 0, "truncated": False}
                        for family in families if family not in {row[0] for row in available})
        return observations, coverage

    def capture(self, asset: str, as_of: int, mode: str = "live", *, source: str | None = None,
                quote: str | None = None, profile: str = "bundle", vigilance_policy: dict | None = None,
                observation_limit: int = 200000, candle_limit: int = 500000,
                include_candle_versions: bool = False) -> dict:
        """Read operation-specific analytical evidence in one SQLite read transaction.

        Explicit native-market filters precede the candle cap. Limits may be lowered
        for bounded callers/tests, never raised above the production resource contract.
        Candle-only operations intentionally do not read global observation history.
        Optional replay history requires an explicit native market and is frozen
        here, rather than reconstructed later from recyclable physical row IDs.
        """
        if asset not in ("BTC", "ETH") or mode not in ("live", "demo", "import"):
            raise ValueError("Unsupported asset or data mode.")
        if type(as_of) is not int:
            raise ValueError("as_of must be UTC epoch seconds.")
        if profile not in ("bundle", "candles", "vigilance"):
            raise ValueError("Capture profile must be bundle, candles or vigilance.")
        if include_candle_versions and (not source or not quote):
            raise ValueError("Candle version capture requires an explicit source and native quote.")
        for name, value in (("observation_limit", observation_limit), ("candle_limit", candle_limit)):
            if type(value) is not int or not 1 <= value <= 500000:
                raise ValueError(f"{name} must be between 1 and 500000.")
        baseline = 3600
        if profile == "vigilance":
            from .vigilance import _policy
            rules = _policy(vigilance_policy)
            baseline = rules["baseline_seconds"]
            observation_limit, candle_limit = min(observation_limit, 100000), min(candle_limit, 20000)
        conditions = ["asset=?", "data_mode=?", "close_time<=?", "observed_at<=?"]
        values = [asset, mode, as_of, as_of]
        for name, value in (("source", source), ("quote", quote)):
            if value is not None:
                conditions.append(f"{name}=?")
                values.append(value)
        with self.connection() as con:
            con.execute("BEGIN")
            watermark = {table: con.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]
                         for table in ("candles", "observations", "events", "runs", "local_records")}
            cte = f"""WITH versions AS (SELECT *,
                ROW_NUMBER() OVER(PARTITION BY source,asset,quote,timeframe,open_time,data_mode
                    ORDER BY observed_at DESC,id DESC) version_rank FROM candles
                WHERE {' AND '.join(conditions)}), latest AS (SELECT *,
                ROW_NUMBER() OVER(PARTITION BY source,asset,quote,data_mode ORDER BY open_time DESC) series_rank
                FROM versions WHERE version_rank=1) """
            candle_coverage = con.execute(cte + """SELECT source,quote,COUNT(*),MIN(open_time),MAX(close_time)
                FROM latest GROUP BY source,quote""", values).fetchall()
            candles = con.execute(cte + "SELECT payload FROM latest "
                                  + ("WHERE series_rank<=48 " if profile == "vigilance" else "")
                                  + "ORDER BY open_time DESC,source,quote LIMIT ?",
                                  [*values, candle_limit + 1]).fetchall()
            if len(candles) > candle_limit and profile != "bundle":
                raise ValueError(f"Required {profile} candle evidence exceeds the {candle_limit}-row capture limit.")
            candle_versions = None
            if include_candle_versions:
                # Retain every eligible vintage: known-at replay needs old timely
                # bars and first later-known bars, not only each latest revision.
                version_limit = min(candle_limit, 100000)
                candle_versions = con.execute(
                    f"SELECT payload FROM candles WHERE {' AND '.join(conditions)} "
                    "ORDER BY open_time,observed_at,id LIMIT ?", [*values, version_limit + 1]).fetchall()
                if len(candle_versions) > version_limit:
                    raise ValueError(f"Required candle versions exceed the {version_limit}-row capture limit.")
            observations, observation_coverage = self._capture_observations(
                con, asset, as_of, mode, profile, source, quote, observation_limit, baseline)
            # Health and calendars are consumed by broad bundles; analytical candle
            # and vigilance profiles omit this unrelated optional evidence entirely.
            events = runs = calendar = []
            counts = {}
            if profile == "bundle":
                events = con.execute("""SELECT payload FROM (SELECT payload,event_time,
                    ROW_NUMBER() OVER(PARTITION BY id ORDER BY observed_at DESC) rn FROM events
                    WHERE observed_at<=? AND event_time>=? AND event_time<=?)
                    WHERE rn=1 ORDER BY event_time LIMIT 101""",
                    (as_of, as_of - 86400, as_of + 60 * 86400)).fetchall()
                runs = con.execute("""SELECT payload FROM (SELECT payload,observed_at,
                    ROW_NUMBER() OVER(PARTITION BY source,task ORDER BY observed_at DESC,id DESC) rn
                    FROM runs WHERE observed_at<=?) WHERE rn=1 ORDER BY observed_at DESC LIMIT 101""",
                    (as_of,)).fetchall()
                calendar = con.execute(f"""SELECT payload FROM (SELECT payload,id,
                    ROW_NUMBER() OVER(PARTITION BY identity ORDER BY {_LOCAL_RECORD_VERSION_ORDER}) rn
                    FROM local_records WHERE kind='calendar' AND observed_at<=?)
                    WHERE rn=1 ORDER BY id DESC LIMIT 501""", (as_of,)).fetchall()
                for table in ("candles", "observations"):
                    counts[table] = con.execute(f"SELECT COUNT(*) FROM {table} WHERE data_mode=? AND observed_at<=?",
                                                (mode, as_of)).fetchone()[0]
                for table in ("events", "runs"):
                    counts[table] = con.execute(f"SELECT COUNT(*) FROM {table} WHERE observed_at<=?",
                                                (as_of,)).fetchone()[0]
        retained_candles = [Candle(**json.loads(row[0])) for row in reversed(candles[:candle_limit])]
        market_coverage = [{"source": venue, "quote": currency, "available": count,
                            "captured": sum(row.source == venue and row.quote == currency for row in retained_candles),
                            "available_start": start, "available_end": end}
                           for venue, currency, count, start, end in candle_coverage]
        return {"watermark": watermark, "candles": retained_candles, "observations": observations,
                "candle_versions": ([Candle(**json.loads(row[0])) for row in candle_versions]
                                    if candle_versions is not None else None),
                "events": [json.loads(row[0]) for row in events[:100]],
                "calendar": [json.loads(row[0]) for row in calendar[:500]],
                "runs": [json.loads(row[0]) for row in runs[:100]], "counts": counts,
                "capture_profile": {"name": profile, "observation_limit": observation_limit,
                                    "candle_limit": candle_limit, "observation_history_days": 730 if profile != "candles" else None,
                                    "baseline_seconds": baseline if profile == "vigilance" else None,
                                    "optional_context": "included" if profile == "bundle" else "not_requested"},
                "truncation": {"candles": len(candles) > candle_limit,
                               "observations": any(row.get("truncated", False) for row in observation_coverage),
                               "events": len(events) > 100, "runs": len(runs) > 100, "calendar": len(calendar) > 500,
                               "candle_coverage": market_coverage, "observation_coverage": observation_coverage}}

    def latest_candle_time(self, source: str, asset: str, mode: str = "live", *, quote: str | None = None) -> int | None:
        with self.connection() as con:
            return con.execute(
                "SELECT MAX(open_time) FROM candles WHERE source=? AND asset=? AND data_mode=?"
                + (" AND quote=?" if quote is not None else ""),
                (source, asset, mode, quote) if quote is not None else (source, asset, mode),
            ).fetchone()[0]

    def save_local_record(self, kind: str, payload: dict) -> dict:
        """Preserve sourced imports and date-only calendars without invented event times."""
        if kind not in ("calendar", "history_import", "fx_reference", "stablecoin_price") or not isinstance(payload, dict):
            raise ValueError("Unsupported local record kind or payload.")
        if kind in ("fx_reference", "stablecoin_price"):
            required = ("record_id", "source", "base", "quote", "source_url", "data_version", "price_type")
            if (any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required)
                    or payload.get("data_mode") not in ("live", "import", "demo")
                    or type(payload.get("observed_at")) is not int or payload["observed_at"] < 0
                    or (payload.get("event_time") is not None and (
                        type(payload["event_time"]) is not int
                        or not 0 <= payload["event_time"] <= payload["observed_at"]))):
                raise ValueError("Quote records require explicit provenance, mode and known observation time.")
        encoded = _json(payload)
        if len(encoded.encode()) > 2_000_000:
            raise ValueError("Local record exceeds the 2 MB limit.")
        semantic = {key: value for key, value in payload.items() if key not in ("observed_at", "imported_at")}
        fingerprint = _hash(semantic)
        identity = str(payload.get("record_id") or payload.get("event_id") or payload.get("uid") or payload.get("id") or fingerprint)
        observed = payload.get("observed_at", int(time.time()))
        with self.connection() as con:
            cursor = con.execute("INSERT OR IGNORE INTO local_records(kind,identity,observed_at,fingerprint,payload) VALUES(?,?,?,?,?)",
                                 (kind, identity, observed, fingerprint, encoded))
        return {"inserted": cursor.rowcount, "identity": identity, "fingerprint": fingerprint}

    def local_records(self, kind: str, as_of: int | None = None, limit: int = 500) -> list[dict]:
        if kind not in ("calendar", "history_import", "fx_reference", "stablecoin_price") or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Use a supported record kind and a limit from 1 to 1000.")
        now = int(time.time()) if as_of is None else as_of
        with self.connection() as con:
            rows = con.execute(f"""SELECT payload FROM (SELECT payload,id,
                ROW_NUMBER() OVER(PARTITION BY identity ORDER BY {_LOCAL_RECORD_VERSION_ORDER}) rn
                FROM local_records WHERE kind=? AND observed_at<=?) WHERE rn=1 ORDER BY id DESC LIMIT ?""",
                (kind, now, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def stored_analysis_snapshot(self, kind: str, *, as_of: int, mode: str,
                                 sources: list[str], quote: str, asset: str | None = None,
                                 history_seconds: int = 172800, limit: int = 5000) -> dict:
        """Read bounded native evidence and its watermark in one SQLite snapshot.

        Derivatives retain every eligible vintage, including revisions, throughout
        the explicit history window. A later known version must not erase evidence
        available at the 4h/24h endpoints. Required evidence exceeding the budget
        raises rather than silently replacing those endpoints with recent rows.
        Quote records keep unknown event times; analytical callers decide whether
        these records support a dated comparison.
        """
        if (kind not in ("derivatives", "quotes") or type(as_of) is not int or as_of < 0
                or mode not in ("live", "import", "demo")
                or not isinstance(sources, list) or not 1 <= len(sources) <= 32
                or any(not isinstance(source, str) or not source or len(source) > 256 for source in sources)
                or len(set(sources)) != len(sources)
                or not isinstance(quote, str) or not quote or len(quote) > 120
                or type(history_seconds) is not int or not 86400 <= history_seconds <= 172800
                or type(limit) is not int or not 1 <= limit <= 5000
                or (kind == "derivatives" and asset not in ("BTC", "ETH"))):
            raise ValueError("Stored analysis requires explicit bounded native selectors, mode and cutoff.")
        placeholders = ",".join("?" for _ in sources)
        with self.connection() as con:
            con.execute("BEGIN")
            watermark = {table: con.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
                         for table in ("observations", "local_records")}
            if kind == "derivatives":
                rows = con.execute(f"""SELECT id,fingerprint,payload FROM observations
                    WHERE asset=? AND data_mode=? AND source IN ({placeholders})
                    AND json_extract(payload,'$.quote')=? AND event_time BETWEEN ? AND ? AND observed_at<=?
                    AND metric IN ('funding_rate_actual','funding_rate_projected','open_interest_amount','open_interest_value')
                    ORDER BY event_time,observed_at,id LIMIT ?""",
                    (asset, mode, *sources, quote, max(0, as_of - history_seconds), as_of, as_of, limit + 1)).fetchall()
                table = "observations"
            else:
                # Filter native pair, source and mode before applying the budget.
                rows = con.execute(f"""WITH eligible AS (SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY kind,json_extract(payload,'$.source'),json_extract(payload,'$.base'),
                                 json_extract(payload,'$.quote')
                    ORDER BY observed_at DESC,id DESC) version_rank FROM local_records
                    WHERE kind IN ('fx_reference','stablecoin_price') AND observed_at<=?
                    AND json_extract(payload,'$.data_mode')=?
                    AND json_extract(payload,'$.source') IN ({placeholders})
                    AND (json_extract(payload,'$.event_time') IS NULL OR json_extract(payload,'$.event_time')<=?)
                    AND ((kind='stablecoin_price' AND json_extract(payload,'$.quote')=?
                          AND json_extract(payload,'$.base') IN ('USDT','USDC'))
                         OR (kind='fx_reference' AND (
                            (json_extract(payload,'$.base')='EUR' AND json_extract(payload,'$.quote')='USD')
                            OR (json_extract(payload,'$.base')='USD' AND json_extract(payload,'$.quote')='EUR')))))
                    SELECT id,fingerprint,payload,kind FROM eligible WHERE version_rank=1
                    ORDER BY observed_at,id LIMIT ?""",
                    (as_of, mode, *sources, as_of, quote, limit + 1)).fetchall()
                table = "local_records"
        if len(rows) > limit:
            raise ValueError(f"Required stored {kind} evidence exceeds the {limit}-row budget; narrow sources or selection.")
        return {"kind": kind, "as_of": as_of, "watermark": watermark,
                "selection": {"asset": asset, "sources": sources.copy(), "quote": quote, "data_mode": mode,
                              "history_seconds": history_seconds if kind == "derivatives" else None},
                "records": [{"input_reference": f"sqlite:{table}:{row['id']}",
                             "fingerprint": row["fingerprint"], "payload": json.loads(row["payload"]),
                             **({"kind": row["kind"]} if kind == "quotes" else {})} for row in rows],
                "truncation": {"truncated": False, "limit": limit, "captured": len(rows)}}

    def put_events(self, events: list[dict], now: int) -> int:
        inserted = 0
        with self.connection() as con:
            for event in events:
                if not event.get("title") or not event.get("source_url", "").startswith("https://"):
                    raise ValueError("Each event requires a title and an HTTPS source URL.")
                timestamp = int(event["event_time"])
                payload = {
                    **event,
                    "event_time": timestamp,
                    "observed_at": now,
                    "origin": "manual_import",
                    "publication_time_verified": False,
                }
                identity = _hash({k: event.get(k) for k in ("title", "event_time", "source_url")})
                prior = con.execute(
                    "SELECT payload FROM events WHERE id=? ORDER BY observed_at DESC LIMIT 1", (identity,)
                ).fetchone()
                if prior and {k: v for k, v in json.loads(prior[0]).items() if k != "observed_at"} == {
                    k: v for k, v in payload.items() if k != "observed_at"
                }:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO events VALUES(?,?,?,?)",
                    (identity, now, timestamp, _json(payload)),
                )
                inserted += 1
        return inserted

    def events(self, as_of: int, days: int = 60) -> list[dict]:
        with self.connection() as con:
            rows = con.execute(
                """SELECT payload FROM (SELECT payload,event_time,
                ROW_NUMBER() OVER(PARTITION BY id ORDER BY observed_at DESC) rn
                FROM events WHERE observed_at<=? AND event_time>=? AND event_time<=?)
                WHERE rn=1 ORDER BY event_time LIMIT 100""",
                (as_of, as_of - 86400, as_of + days * 86400),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def export_parquet(self, output: Path) -> dict:
        """Export latest versions with provenance. SQL is internal, not agent-provided."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        output.mkdir(parents=True, exist_ok=True)
        result = {}
        with self.connection() as con:
            for table, partition in (
                ("candles", "source,asset,quote,timeframe,open_time,data_mode"),
                ("observations", "source,asset,metric,event_time,data_mode,dimension"),
            ):
                records = con.execute(f"""SELECT payload FROM (SELECT payload,
                    ROW_NUMBER() OVER(PARTITION BY {partition} ORDER BY observed_at DESC,id DESC) rn
                    FROM {table}) WHERE rn=1""").fetchall()
                rows = [json.loads(r[0]) for r in records]
                for row in rows:
                    if "metadata" in row:
                        row["metadata_json"] = _json(row.pop("metadata"))
                if rows:
                    path = output / f"{table}.parquet"
                    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
                    result[table] = {"path": str(path.resolve()), "rows": len(rows)}
        return result
