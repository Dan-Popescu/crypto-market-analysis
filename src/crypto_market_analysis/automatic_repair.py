"""Finite opt-in repair cycles for named import datasets; inspection never recovers.

Task checkpoints are separate from manual repairs. A checkpoint precedes each
request, and immutable capture journals remain HistoryDataset's responsibility.
After a crash, coverage is read again before retrying an unfinished task.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from .evidence import canonical_json as _json
from .history_dataset import MARKETS, DatasetConfig, HistoryDataset, UnsupportedMarket
from .storage import database_table_names as _tables
from .storage import read_database_if_present as _read

HOUR = 3600
_TABLE = "history_auto_repairs"


@dataclass(frozen=True)
class AutoRepairRequest:
    markets: tuple[tuple[str, str], ...] = MARKETS
    max_ranges: int = 8
    max_hours: int = 720
    request_budget: int = 4
    retry_seconds: int = 60

    def __post_init__(self):
        markets = tuple(tuple(m) for m in self.markets)
        if not markets or len(markets) > 4 or len(set(markets)) != len(markets) or any(m not in MARKETS for m in markets):
            raise ValueError("Select distinct native Kraken BTC/ETH EUR/USD markets.")
        object.__setattr__(self, "markets", markets)
        for key, high in (("max_ranges", 128), ("max_hours", 2880), ("request_budget", 100), ("retry_seconds", 86400)):
            if type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= high:
                raise ValueError(f"{key} must be an integer from 1 to {high}.")


def normalize_auto_repair_request(request=None):
    """Accept the bounded public JSON request or an already validated request."""
    if request is None:
        return AutoRepairRequest()
    if isinstance(request, AutoRepairRequest):
        return request
    if not isinstance(request, dict):
        raise ValueError("Automatic repair request must be a JSON object.")
    try:
        return AutoRepairRequest(**request)
    except TypeError as exc:
        raise ValueError("Invalid automatic repair request fields or markets.") from exc


def _identity(database, mode):
    if mode not in ("live", "import", "demo"):
        raise ValueError("Unsupported data mode.")
    database = Path(database).expanduser().resolve()
    marker = database.parent / "dataset.json"
    if not marker.is_file():
        return database, None, "not_configured"
    if marker.stat().st_size > 2_000_000:
        raise ValueError("Dataset identity exceeds the 2 MB bound.")
    config = DatasetConfig(**json.loads(marker.read_text()))
    if Path(config.directory).expanduser().resolve() != database.parent or database.name != "observer.sqlite":
        raise ValueError("Configured store does not match the named dataset identity.")
    return database, config, "available" if mode == "import" else "selection_mismatch"


def _present(con, config, asset, quote, now):
    return {r[0] for r in con.execute("""SELECT DISTINCT open_time FROM candles
        WHERE source='kraken' AND data_mode='import' AND timeframe='1h' AND asset=? AND quote=?
        AND open_time>=? AND open_time<? AND observed_at<=? AND close_time<=?""",
        (asset, quote, config.target_start, config.target_end, now, now))}


def _task_id(asset, quote, start, end):
    return hashlib.sha256(_json([asset, quote, start, end]).encode()).hexdigest()


def _runs(hours):
    start = previous = None
    for hour in hours:
        if previous is not None and hour != previous + HOUR:
            yield start, previous + HOUR
            start = None
        if start is None:
            start = hour
        previous = hour
    if start is not None:
        yield start, previous + HOUR


def plan_auto_repairs(database, request: AutoRepairRequest | None = None, *, mode="import", now=None):
    """Discover bounded requests, exact target gaps and current durable checkpoints.

    No writes, directory creation, schema initialization or pending replay occur.
    Retention uses Kraken's most recent 720 entries including the open hour, so
    only the preceding 719 closed hours are considered API recoverable.
    """
    request = normalize_auto_repair_request(request)
    now = int(time.time()) if now is None else now
    if type(now) is not int or now < 0:
        raise ValueError("Expected a nonnegative integer UTC epoch second.")
    database, config, status = _identity(database, mode)
    base = {"status": status, "read_only": True, "data_mode": mode, "as_of": now,
            "tasks": [], "markets": [], "requests_planned": 0, "hours_planned": 0,
            "pending_recovery": "not_inspected_or_replayed"}
    if status != "available":
        return base
    if request.request_budget > config.max_requests:
        raise ValueError("Automatic request budget exceeds the dataset request bound.")
    base.update(dataset=config.name, source="kraken", target_start=config.target_start, target_end=config.target_end)
    closed = now // HOUR * HOUR
    retention = max(0, closed - 719 * HOUR)
    base.update(closed_before=closed, recoverable_start=retention)
    with _read(database) as con:
        tables = _tables(con)
        if not {"candles", "history_repairs"} <= tables:
            base["status"] = "database_unavailable"
            return base
        saved = [json.loads(r[0]) for r in con.execute(f"SELECT payload FROM {_TABLE} ORDER BY rowid")] if _TABLE in tables else []
        manual = {r[0]: json.loads(r[1]) for r in con.execute("SELECT market,payload FROM history_repairs")}
        candidates = []
        for asset, quote in request.markets:
            present = _present(con, config, asset, quote, now)
            missing = [h for h in range(config.target_start, config.target_end, HOUR) if h not in present]
            recoverable = [h for h in missing if retention <= h < closed]
            active = manual.get(asset + quote, {}).get("status") not in (None, "complete")
            summary = {"asset": asset, "quote": quote, "remaining_hours": len(missing),
                       "requires_archives_hours": sum(h < retention for h in missing),
                       "not_closed_hours": sum(h >= closed for h in missing),
                       "recoverable_hours": len(recoverable), "manual_repair_active": active}
            gaps = []
            for kind, hours in (("requires_archives", [h for h in missing if h < retention]),
                                ("recoverable", recoverable), ("not_closed", [h for h in missing if h >= closed])):
                gaps.extend({"start": start, "end": end, "status": kind} for start, end in _runs(hours))
            summary.update(gaps=gaps[:request.max_ranges], gap_ranges_total=len(gaps),
                           gap_ranges_omitted=max(0, len(gaps) - request.max_ranges))
            base["markets"].append(summary)
            if active:
                continue
            own = [s for s in saved if (s["asset"], s["quote"]) == (asset, quote)]
            covered = set()
            for state in own:
                covered.update(range(state["start"], state["end"], HOUR))
                hours = [h for h in recoverable if state["start"] <= h < state["end"]]
                if hours and state["status"] != "unsupported" and state.get("retry_at", 0) <= now:
                    candidates.append((state, hours))
            fresh = [h for h in recoverable if h not in covered]
            for start, end in _runs(fresh):
                # Each durable range stays bounded even when the target spans years.
                for chunk in range(start, end, 720 * HOUR):
                    stop = min(end, chunk + 720 * HOUR)
                    state = {"id": _task_id(asset, quote, chunk, stop), "asset": asset, "quote": quote,
                             "start": chunk, "end": stop, "status": "pending", "requests": 0, "retry_at": 0}
                    candidates.append((state, list(range(chunk, stop, HOUR))))
        hours_left = request.max_hours
        # Give each native market a turn before selecting its second range.
        turns = {}
        ordered = []
        for state, hours in candidates:
            market = (state["asset"], state["quote"])
            turn = turns.get(market, 0)
            turns[market] = turn + 1
            ordered.append((turn, request.markets.index(market), state, hours))
        candidates = [(state, hours) for _, _, state, hours in sorted(ordered, key=lambda item: item[:2])]
        selected = candidates[:min(request.max_ranges, request.request_budget, request.max_hours)]
        ranges = []
        for state, hours in selected:
            start, end = next(_runs(hours))
            capacity = min((end - start) // HOUR, 720, config.max_rows)
            ranges.append((state, hours, start, capacity))
        # Share the finite hour budget across the selected requests. Short gaps
        # release their unused share to longer gaps in the same cycle.
        allocations = [0] * len(ranges)
        active = list(range(len(ranges)))
        while hours_left and active:
            share = max(1, hours_left // len(active))
            for index in active:
                count = min(share, ranges[index][3] - allocations[index], hours_left)
                allocations[index] += count
                hours_left -= count
            active = [index for index in active if allocations[index] < ranges[index][3]]
        for (state, hours, start, _), count in zip(ranges, allocations, strict=True):
            base["tasks"].append({**state, "request_start": start, "request_end": start + count * HOUR,
                                  "remaining_hours": len(hours), "request_hours": count})
        base["hours_planned"] = request.max_hours - hours_left
        base["requests_planned"] = len(base["tasks"])
    return base


def automatic_repair_status(database, *, mode="import", limit=20):
    """Current bounded mutable checkpoints, without initialization or recovery."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100.")
    database, config, status = _identity(database, mode)
    result = {"status": status, "data_mode": mode, "read_only": True, "tasks": [],
              "time_basis": "current_mutable_checkpoints; no_historical_cutoff",
              "pending_recovery": "not_inspected_or_replayed"}
    if status != "available":
        return result
    result["dataset"] = config.name
    with _read(database) as con:
        if _TABLE not in _tables(con):
            result.update(status="not_recorded", tasks_total=0, tasks_omitted=0)
        else:
            total = con.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
            result.update(tasks=[json.loads(r[0]) for r in con.execute(
                f"SELECT payload FROM {_TABLE} ORDER BY rowid DESC LIMIT ?", (limit,))],
                tasks_total=total, tasks_omitted=max(0, total - limit))
    return result


def _retry_at(exc, now, delay):
    """Recognize explicit adapter seconds and standard numeric/date Retry-After."""
    value = getattr(exc, "retry_after", None)
    headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
    if value is None and headers:
        value = headers.get("Retry-After", headers.get("retry-after"))
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return max(now + delay, now + math.ceil(seconds))
    except (ValueError, TypeError):
        if isinstance(value, str):
            try:
                return max(now + delay, math.ceil(parsedate_to_datetime(value).timestamp()))
            except (ValueError, TypeError, OverflowError):
                pass
    return now + delay


def run_auto_repairs(database, request: AutoRepairRequest | None = None, *, apply=False,
                     fetch=None, mode="import", clock=time.time):
    """Run one explicit finite cycle or return its read-only plan by default.

    Retry deadlines apply between invocations, with no sleeping or background
    scheduler. A transport failure in one native market does not stop the others.
    """
    if type(apply) is not bool:
        raise ValueError("apply must be an explicit boolean.")
    request = normalize_auto_repair_request(request)
    plan = plan_auto_repairs(database, request, mode=mode, now=int(clock()))
    if not apply or plan["status"] != "available":
        return plan
    if fetch is None:
        raise ValueError("Explicit apply requires an injected REST fetcher.")
    _, config, _ = _identity(database, mode)
    dataset = HistoryDataset(config, clock=clock)
    dataset.check_write_budget()
    with dataset.store.connection() as con:
        con.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    _reconcile(dataset, config, request.markets, int(clock()))
    # Replay may have restored candles, so do not request already recovered gaps.
    plan = plan_auto_repairs(database, request, mode=mode, now=int(clock()))
    outcomes = []
    for task in plan["tasks"]:
        now = int(clock())
        # Re-evaluate moving retention, closure, coverage and manual ownership.
        fresh = plan_auto_repairs(database, request, mode=mode, now=now)
        candidate = next((t for t in fresh["tasks"] if t["id"] == task["id"]), None)
        if candidate is None:
            continue
        candidate["request_end"] = min(candidate["request_end"], task["request_end"])
        if candidate["request_start"] >= candidate["request_end"]:
            continue
        candidate["request_hours"] = (candidate["request_end"] - candidate["request_start"]) // HOUR
        state = {k: v for k, v in candidate.items() if k not in ("request_start", "request_end", "request_hours")}
        with dataset.store.connection() as con:
            present_before = _present(con, config, state["asset"], state["quote"], now)
        requested_missing_before = set(range(candidate["request_start"], candidate["request_end"], HOUR)) - present_before
        state.update(status="inflight", requests=state["requests"] + 1, updated_at=now,
                     remaining_hours=sum(h not in present_before for h in range(state["start"], state["end"], HOUR)),
                     retry_at=now + request.retry_seconds)
        _save(dataset, state)
        try:
            result = dataset.append_rest(state["asset"], state["quote"], fetch,
                                         since=candidate["request_start"], until=candidate["request_end"],
                                         limit=candidate["request_hours"])
            state.update(last_capture=result["id"], status="pending")
            state.pop("error_type", None)
        except Exception as exc:
            failures = state.get("consecutive_failures", 0) + 1
            delay = min(86400, request.retry_seconds * 2 ** min(failures - 1, 10))
            state.update(status="unsupported" if isinstance(exc, UnsupportedMarket) else "error",
                         error_type=type(exc).__name__, consecutive_failures=failures,
                         retry_deadline_basis=getattr(exc, "retry_after_basis", "exception_metadata_or_local_backoff"),
                         retry_at=_retry_at(exc, int(clock()), delay))
        finished = int(clock())
        with dataset.store.connection() as con:
            present = _present(con, config, state["asset"], state["quote"], finished)
        missing = [h for h in range(state["start"], state["end"], HOUR) if h not in present]
        state.update(remaining_hours=len(missing), updated_at=finished)
        if not missing:
            state.update(status="complete", retry_at=0, consecutive_failures=0)
        elif state["status"] == "pending":
            if all(h < (finished // HOUR - 719) * HOUR for h in missing):
                state["status"] = "requires_archives"
            # Count progress only when a requested missing hour materializes;
            # moving retention never changes this before/after coverage test.
            elif requested_missing_before.isdisjoint(present):
                state["status"] = "unknown_gap"
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1 if state["status"] == "unknown_gap" else 0
            delay = min(86400, request.retry_seconds * 2 ** min(max(0, state["consecutive_failures"] - 1), 10))
            state["retry_at"] = finished + delay
        _save(dataset, state)
        outcomes.append(state)
    return {**plan_auto_repairs(database, request, mode=mode, now=int(clock())), "read_only": False,
            "applied": True, "requests_attempted": len(outcomes), "outcomes": outcomes,
            "pending_recovery": "replayed_before_cycle"}


def _save(dataset, state):
    dataset.check_write_budget(len(_json(state).encode()) * 2)
    with dataset.store.connection() as con:
        con.execute(f"INSERT OR REPLACE INTO {_TABLE} VALUES(?,?)", (state["id"], _json(state)))


def _reconcile(dataset, config, markets, now):
    with dataset.store.connection() as con:
        saved = [json.loads(r[0]) for r in con.execute(f"SELECT payload FROM {_TABLE}")]
        present = {(asset, quote): _present(con, config, asset, quote, now) for asset, quote in markets}
    for state in saved:
        own = present.get((state["asset"], state["quote"]))
        if own is None:
            continue
        missing = [h for h in range(state["start"], state["end"], HOUR) if h not in own]
        state.update(remaining_hours=len(missing), updated_at=now)
        if not missing:
            state.update(status="complete", retry_at=0)
        elif all(h < (now // HOUR - 719) * HOUR for h in missing):
            state["status"] = "requires_archives"
        _save(dataset, state)
