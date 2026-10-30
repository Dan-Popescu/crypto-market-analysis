"""Finite resumable native spot history into an ordinary configured store.

Cursors are advisory: every fetch begins at the oldest *actual* missing hour.
A committed page therefore survives interruption before its checkpoint update.
No historical listing date, continuous coverage or redistribution right is assumed.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import replace
from email.utils import parsedate_to_datetime
from pathlib import Path

from .asset_universe import validate_asset
from .collectors import ExchangeClients, parse_candles, select_market
from .dataset_lock import DatasetBusy, store_writer_lock
from .http_accounting import accounting_scope, instrument_exchange, merge_measurements, public_error
from .models import CollectionResult
from .storage import Store, database_table_names, read_database_if_present

HOUR = 3600
MAX_HOURS = 24 * 366 * 20
SOURCES = {
    "coinbase": {"page_cap": 300, "documentation": "https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-candles"},
    "bitstamp": {"page_cap": 1000, "documentation": "https://www.bitstamp.net/api/"},
}
class BackfillPageError(ValueError):
    """A local normalized-page validation failure safe to retain."""


DEFAULTS = {"max_pages": 4, "max_requests": 8, "max_rows": 1200,
            "max_bytes": 2 * 1024 * 1024, "http_max_requests": 32,
            "deadline_seconds": 120, "backoff_seconds": 60}
MAXIMUMS = {"max_pages": 32, "max_requests": 64, "max_rows": 32000,
            "max_bytes": 32 * 1024 * 1024, "http_max_requests": 256,
            "deadline_seconds": 300, "backoff_seconds": 21600}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _retry_deadline(exc, now, delay, *, client=None):
    """Retain only a bounded numeric deadline and its evidence basis.

    Current exception metadata takes precedence over potentially stale CCXT
    client headers. Expired, malformed and nonfinite values cannot shorten the
    local exponential cooldown. No response headers or provider text survive.
    """
    deadline, basis = now + delay, "local_exponential_backoff"
    candidates = []
    absolute = getattr(exc, "retry_at", None)
    if type(absolute) in (int, float):
        candidates.append((absolute, True, "exception_retry_at"))
    retry = getattr(exc, "retry_after", None)
    if retry is not None:
        origin = ("ccxt_client_last_response_header_unverified"
                  if getattr(exc, "retry_after_basis", None) == "ccxt_client_last_response_header_unverified"
                  else "exception_retry_after")
        candidates.append((retry, False, origin))
    headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
    header = headers.get("Retry-After", headers.get("retry-after")) if hasattr(headers, "get") else None
    if header is not None:
        candidates.append((header, False, "exception_response_retry_after"))
    if not candidates:
        headers = getattr(client, "last_response_headers", None)
        header = headers.get("Retry-After", headers.get("retry-after")) if hasattr(headers, "get") else None
        if header is not None:
            candidates.append((header, False, "ccxt_client_last_response_header_unverified"))
    for value, absolute, origin in candidates:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        if isinstance(value, str) and len(value) > 128:
            continue
        try:
            number = float(value)
            candidate = number if absolute else now + number
            if not absolute and number < 0:
                continue
        except (ValueError, TypeError, OverflowError):
            if absolute or not isinstance(value, str):
                continue
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    continue
                candidate = parsed.timestamp()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(candidate) and deadline < candidate <= 253402300799:
            deadline, basis = candidate, origin
    return deadline, basis


def normalize_backfill_request(request):
    """Validate bounded public JSON; database paths belong to configuration only."""
    if not isinstance(request, dict) or len(_json(request).encode()) > 65536:
        raise ValueError("Backfill request must be a JSON object of at most 64 KiB.")
    allowed = {"source", "markets", "start", "end", "mode", *DEFAULTS}
    if set(request) - allowed:
        raise ValueError("Unknown history backfill request fields.")
    source = request.get("source")
    if source not in SOURCES:
        raise ValueError("Backfill source must be coinbase or bitstamp.")
    if request.get("mode", "import") != "import":
        raise ValueError("Retrospective history requires import mode.")
    markets = request.get("markets")
    if not isinstance(markets, list) or not 1 <= len(markets) <= 4:
        raise ValueError("Select one to four explicit native markets.")
    normalized = []
    for market in markets:
        if not isinstance(market, dict) or set(market) != {"asset", "quote"}:
            raise ValueError("Each market requires only asset and quote.")
        validate_asset(market["asset"])
        if market["quote"] not in ("EUR", "USD"):
            raise ValueError("Select native EUR/USD spot markets.")
        if market in normalized:
            raise ValueError("Duplicate native markets are not allowed.")
        normalized.append(dict(market))
    for field in ("start", "end"):
        if type(request.get(field)) is not int or request[field] < 0 or request[field] % HOUR:
            raise ValueError("start and end must be explicit aligned UTC epoch seconds.")
    if not 0 < request["end"] - request["start"] <= MAX_HOURS * HOUR:
        raise ValueError("Backfill range must be positive and at most twenty years.")
    result = {"source": source, "markets": normalized, "start": request["start"],
              "end": request["end"], "mode": "import"}
    for field, default in DEFAULTS.items():
        value = request.get(field, default)
        if type(value) is not int or not 1 <= value <= MAXIMUMS[field]:
            raise ValueError(f"{field} must be an integer from 1 to {MAXIMUMS[field]}.")
        result[field] = value
    return result


def _path(database):
    path = Path(database).expanduser().resolve()
    if (path.parent / "dataset.json").exists():
        raise ValueError("Native backfill refuses named history datasets; use an ordinary configured import store.")
    return path


def _identity(request):
    selection = {key: request[key] for key in ("source", "markets", "start", "end", "mode")}
    return hashlib.sha256(_json(selection).encode()).hexdigest()


def _key(market):
    return market["asset"] + "/" + market["quote"]


def _coverage(con, request, market, now):
    """Read only bounded distinct event times, never a newest-time shortcut."""
    end = max(request["start"], min(request["end"], now // HOUR * HOUR))
    cursor = request["start"]
    covered = 0
    missing = None
    gap_end = end
    if con is not None and "candles" in database_table_names(con):
        rows = con.execute("""SELECT DISTINCT open_time FROM candles WHERE source=? AND asset=?
            AND quote=? AND timeframe='1h' AND data_mode='import' AND open_time>=?
            AND open_time<? AND close_time=open_time+3600 AND observed_at>=close_time
            ORDER BY open_time LIMIT ?""", (request["source"], market["asset"], market["quote"],
                                            request["start"], end, MAX_HOURS + 1))
        for row in rows:
            stamp = row[0]
            if stamp % HOUR:
                continue
            if missing is None and stamp > cursor:
                missing, gap_end = cursor, stamp
            cursor = stamp + HOUR
            covered += 1
    if missing is None and cursor < end:
        missing = cursor
    expected = max(0, (end - request["start"]) // HOUR)
    return {"covered_hours": covered, "missing_hours": expected - covered,
            "oldest_missing": missing, "gap_end": gap_end,
            "unclosed_hours": (request["end"] - end) // HOUR if end < request["end"] else 0}


def _job(con, job_id):
    if con is None or "history_backfill_jobs" not in database_table_names(con):
        return None
    row = con.execute("SELECT payload FROM history_backfill_jobs WHERE job_id=?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


def plan_history_backfill(database, request, *, now=None):
    """Noninitializing, network-free inspection; no checkpoint recovery."""
    request = normalize_backfill_request(request)
    path = _path(database)
    now = int(time.time() if now is None else now)
    with read_database_if_present(path) as con:
        job = _job(con, _identity(request))
        markets = [{**market, **_coverage(con, request, market, now),
                    "checkpoint": (job or {}).get("markets", {}).get(_key(market))}
                   for market in request["markets"]]
    return {"status": "planned", "applied": False, "job_id": _identity(request),
            "request": request, "store_present": path.is_file(), "markets": markets,
            "round_robin_cursor": (job or {}).get("cursor", 0),
            "catalogue_checkpoint": (job or {}).get("catalogue"),
            "source_contract": {**SOURCES[request["source"]], "adapter": "ccxt public native spot OHLCV",
                                "earliest_history": "unknown", "rights": "intended-use review required"},
            "byte_budget_basis": "normalized_page_json_admission", "wire_bytes": None,
            "row_budget_basis": "requested_and_admitted_ohlcv_rows",
            "archive_route": {"automatic_download": False, "guidance":
                "For unavailable history use manually obtained verified official archives: review source, native pair, member, row count, time range and disk preflight before bounded history-dataset stage into its own named Kraken dataset. Archive rights remain separate."}}


def _save_job(store, job):
    encoded = _json(job)
    with store.connection() as con:
        store.check_write_budget(con, len(encoded.encode()))
        con.execute("INSERT INTO history_backfill_jobs VALUES(?,?,?) ON CONFLICT(job_id) "
                    "DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                    (job["job_id"], job["updated_at"], encoded))


def _receipt(clock):
    stamp = clock()
    if isinstance(stamp, bool) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError("Receipt clock must return a nonnegative finite UTC time.")
    return int(stamp)


def _page(rows, source, market, receipt, start, end, limit, byte_limit):
    if not isinstance(rows, list) or len(rows) > limit:
        raise BackfillPageError("Provider page exceeds its requested row budget or is not a list.")
    encoded = _json(rows).encode()
    if len(encoded) > byte_limit:
        raise BackfillPageError("Provider page exceeds remaining normalized JSON byte budget.")
    # Reuse the shared parser, then demand one exact canonical input row per result.
    # The general collector's permissive filtering must not turn invalid history
    # into apparent complete backfill coverage.
    for row in rows:
        if (not isinstance(row, (list, tuple)) or len(row) < 6
                or type(row[0]) is not int or row[0] % (HOUR * 1000)
                or not start * 1000 <= row[0] < end * 1000
                or any(isinstance(value, bool) for value in row[1:6])):
            raise BackfillPageError("Provider page contains noncanonical or out-of-range OHLCV.")
    candles = parse_candles(rows, source, market["asset"], market["quote"], receipt, start)
    if len(candles) != len(rows):
        raise BackfillPageError("Provider page contains invalid, duplicate or unclosed OHLCV.")
    return [replace(candle, data_mode="import") for candle in candles], len(encoded)


def run_history_backfill(database, request, *, apply=False, clients=None, clock=time.time):
    """Apply one explicit finite cycle, with durable per-native-market checkpoints."""
    request = normalize_backfill_request(request)
    path = _path(database)
    if not apply:
        return plan_history_backfill(path, request, now=_receipt(clock))
    try:
        with store_writer_lock(path, create=True):
            return _run(path, request, clients, clock)
    except DatasetBusy:
        return {"status": "busy", "applied": False, "job_id": _identity(request)}


def _run(path, request, clients, clock):
    # Import lazily so a read-only plan/status never initializes transport state.
    from .http_limits import HTTPPolicy, http_budget_scope

    initial = plan_history_backfill(path, request, now=_receipt(clock))
    eligible = [market for market in initial["markets"] if market["oldest_missing"] is not None
                and not ((market["checkpoint"] or {}).get("next_retry", 0) > _receipt(clock)
                         and (market["checkpoint"] or {}).get("start", -1) <= market["oldest_missing"]
                         < (market["checkpoint"] or {}).get("end", -1))]
    if ((initial.get("catalogue_checkpoint") or {}).get("next_retry", 0) > _receipt(clock)):
        return {**initial, "status": "deferred", "applied": True,
                "cycle": {"logical_requests": 0, "pages": 0, "rows": 0,
                          "normalized_page_bytes": 0, "attempts": []}}
    if not eligible:
        return {**initial, "status": "deferred" if any(m["missing_hours"] for m in initial["markets"])
                else "complete" if not any(m["unclosed_hours"] for m in initial["markets"]) else "unclosed",
                "applied": True, "cycle": {"logical_requests": 0, "pages": 0, "rows": 0,
                                           "normalized_page_bytes": 0, "attempts": []}}
    store = Store(path)
    with store.connection() as con:
        con.execute("CREATE TABLE IF NOT EXISTS history_backfill_jobs "
                    "(job_id TEXT PRIMARY KEY,updated_at INTEGER NOT NULL,payload TEXT NOT NULL)")
        job = _job(con, _identity(request))
    now = _receipt(clock)
    job = job or {"job_id": _identity(request), "request": request, "cursor": 0,
                  "markets": {}, "created_at": now}
    job["request"] = request
    used = {"logical_requests": 0, "pages": 0, "rows": 0, "normalized_page_bytes": 0}
    attempts = []
    measurements = []
    owned = clients is None
    # This job-level label describes the latest invocation. Individual page
    # records and checkpoints retain the origin of their own actual capture.
    job["capture_evidence"] = "public_backfill_adapter" if owned else "injected_clients_unqualified"
    pool = ExchangeClients() if owned else clients
    try:
        with http_budget_scope(HTTPPolicy(max_requests=request["http_max_requests"],
                                          deadline_seconds=request["deadline_seconds"])) as budget:
            # One catalogue attempt is included in the adapter budget. Reserve
            # durable intent before catalogue transport as well as every page.
            job["updated_at"] = now
            job["status"] = "catalogue_inflight"
            failures = job.get("catalogue", {}).get("failures", 0)
            catalogue_delay = min(request["backoff_seconds"] * 2 ** min(failures, 9), 21600)
            catalogue_retry, catalogue_basis = now + catalogue_delay, "local_exponential_backoff"
            client = None
            job["catalogue"] = {"status": "inflight", "next_retry": catalogue_retry,
                                "retry_deadline_basis": catalogue_basis, "failures": failures, "updated_at": now}
            _save_job(store, job)
            used["logical_requests"] += 1
            catalogue_error = None
            with accounting_scope(request["source"]) as meter:
                try:
                    budget.check()
                    client = pool.client(request["source"])
                    instrument_exchange(client, request["source"])
                    # Caller-owned transports are an unverified subset even if a
                    # Requests session happens to exist on the injected object.
                    if not owned:
                        budget.unsupported()
                        meter.coverage(request["source"], instrumented=False)
                    markets = pool.markets(request["source"], client)
                    if not isinstance(markets, dict) or not markets:
                        raise ValueError("Empty public market catalogue.")
                except Exception as exc:
                    catalogue_error = public_error(exc, include_message=isinstance(exc, BackfillPageError))
                    catalogue_retry, catalogue_basis = _retry_deadline(
                        exc, _receipt(clock), catalogue_delay, client=client)
                    if catalogue_basis != "local_exponential_backoff":
                        catalogue_error += f" retry_at={catalogue_retry}"
                measured = meter.snapshot(request["source"])
            measurements.append(measured)
            record = CollectionResult(request["source"], http_accounting=measured,
                                      errors=[catalogue_error] if catalogue_error else [])
            store.record_run(record, "history_backfill_catalogue", _receipt(clock), data_mode="import")
            job["catalogue"].update({"status": "error" if catalogue_error else "ready",
                                     "error": catalogue_error, "http_accounting": measured,
                                     "next_retry": catalogue_retry if catalogue_error else 0,
                                     "retry_deadline_basis": catalogue_basis,
                                     "failures": failures + 1 if catalogue_error else 0})
            if catalogue_error:
                attempts.append({"status": "catalogue_error", "error": catalogue_error})
            else:
                _fetch_turns(store, request, job, client, markets, clock, used, attempts, measurements, owned)
            job["http_budget"] = budget.snapshot()
    finally:
        if owned:
            pool.close()
    job["updated_at"] = _receipt(clock)
    job["status"] = "cycle_complete"
    job["last_cycle"] = {**used, "attempts": attempts, "http_budget": job.get("http_budget"),
                         "http_accounting": merge_measurements(measurements, endpoint_limit=32)}
    _save_job(store, job)
    result = plan_history_backfill(path, request, now=_receipt(clock))
    return {**result, "status": "complete" if all(m["missing_hours"] == 0 and not m["unclosed_hours"]
                                                     for m in result["markets"]) else "incomplete",
            "applied": True, "cycle": job["last_cycle"]}


def _fetch_turns(store, request, job, client, catalogue, clock, used, attempts, measurements, owned):
    from .http_limits import HTTPBudgetExceeded, check_http_budget

    blocked = set()
    count = len(request["markets"])
    while (used["pages"] < request["max_pages"] and used["logical_requests"] < request["max_requests"]
           and used["rows"] < request["max_rows"] and len(blocked) < count):
        try:
            check_http_budget()
        except HTTPBudgetExceeded:
            used["refused_operations"] = used.get("refused_operations", 0) + 1
            break
        index = job["cursor"] % count
        market = request["markets"][index]
        key = _key(market)
        job["cursor"] = (index + 1) % count
        if key in blocked:
            continue
        now = _receipt(clock)
        checkpoint = job["markets"].get(key, {})
        with read_database_if_present(store.path) as con:
            coverage = _coverage(con, request, market, now)
        start = coverage["oldest_missing"]
        if start is None:
            job["markets"][key] = {**checkpoint, "status": "complete" if not coverage["unclosed_hours"] else "unclosed", "updated_at": now}
            blocked.add(key)
            continue
        # Completed coverage wins after a process died between commit and cursor save.
        if (checkpoint.get("next_retry", 0) > now
                and checkpoint.get("start", -1) <= start < checkpoint.get("end", -1)):
            blocked.add(key)
            continue
        spot = select_market(catalogue, market["asset"], quotes=(market["quote"],))
        if not spot or not getattr(client, "has", {}).get("fetchOHLCV"):
            job["markets"][key] = {"status": "unsupported", "updated_at": now,
                                    "reason": "No active native spot pair or public OHLCV capability."}
            blocked.add(key)
            continue
        share = max(1, (request["max_rows"] - used["rows"]) // max(1, count - len(blocked)))
        limit = min(SOURCES[request["source"]]["page_cap"], share,
                    (coverage["gap_end"] - start) // HOUR)
        end = start + limit * HOUR
        checkpoint = {"status": "inflight", "start": start, "end": end, "updated_at": now,
                      "capture_evidence": "public_backfill_adapter" if owned else "injected_clients_unqualified",
                      "failures": checkpoint.get("failures", 0),
                      "next_retry": now + request["backoff_seconds"], "symbol": spot["symbol"], "market_id": spot.get("id")}
        job["markets"][key] = checkpoint
        job["updated_at"] = now
        _save_job(store, job)  # Includes round-robin turn before transport.
        used["pages"] += 1
        used["logical_requests"] += 1
        error = None
        hard_limit = False
        page_exception = None
        candles = []
        with accounting_scope(request["source"]) as meter:
            instrument_exchange(client, request["source"])
            if not owned:
                meter.coverage(request["source"], instrumented=False)
            try:
                # CCXT native adapters consume until in milliseconds; disable
                # implicit pagination and Coinbase's authenticated endpoint.
                params = {"until": end * 1000 - 1}
                if request["source"] == "coinbase":
                    params.update({"paginate": False, "usePrivate": False})
                rows = client.fetch_ohlcv(spot["symbol"], "1h", since=start * 1000,
                                          limit=limit, params=params)
                receipt = _receipt(clock)
                candles, page_bytes = _page(rows, request["source"], market, receipt, start, end,
                                            limit, request["max_bytes"] - used["normalized_page_bytes"])
                used["rows"] += len(rows)
                used["normalized_page_bytes"] += page_bytes
                store.put_candles(candles)
                checkpoint["response_sha256"] = hashlib.sha256(_json(rows).encode()).hexdigest()
                checkpoint["data_version"] = "ccxt-native-ohlcv-v1"
                checkpoint["captured_at"] = receipt
                checkpoint["validated_rows"] = len(candles)
                store.save_local_record("history_import", {
                    "record_id": f"{job['job_id']}:{key}:{start}:{end}:{checkpoint['response_sha256']}",
                    "source": request["source"], "source_url": SOURCES[request["source"]]["documentation"],
                    "data_mode": "import", "observed_at": receipt, "captured_at": receipt,
                    "capture_evidence": checkpoint["capture_evidence"], "validated_rows": len(candles),
                    "source_format": checkpoint["data_version"], "response_sha256": checkpoint["response_sha256"],
                    "asset": market["asset"], "quote": market["quote"], "symbol": spot["symbol"],
                    "market_id": spot.get("id"),
                    "start": start, "end": end, "rows": len(candles),
                    "raw_evidence_retained": False, "normalized_response_hash_basis": "ccxt OHLCV rows"})
                # Only exact contiguous completion advances the retry state.
                complete = [c.open_time for c in candles] == list(range(start, end, HOUR))
                if not complete:
                    error = "unknown_gap: empty or partial page; missing hours remain unknown"
            except Exception as exc:
                hard_limit = isinstance(exc, HTTPBudgetExceeded)
                error = public_error(exc, include_message=isinstance(exc, BackfillPageError))
                page_exception = exc
            measured = meter.snapshot(request["source"])
        measurements.append(measured)
        receipt = _receipt(clock)
        if error:
            failures = checkpoint["failures"] + 1
            delay = min(request["backoff_seconds"] * 2 ** min(failures - 1, 9), 21600)
            retry_deadline, retry_basis = _retry_deadline(
                page_exception, receipt, delay, client=client if page_exception else None)
            if retry_basis != "local_exponential_backoff":
                error += f" retry_at={retry_deadline}"
        record = CollectionResult(request["source"], candles=candles,
                                  errors=[error] if error else [], http_accounting=measured)
        store.record_run(record, "history_backfill", receipt, data_mode="import")
        checkpoint.update({"status": "unknown_gap" if error else "captured", "updated_at": receipt,
                           "error": error, "rows": len(candles), "http_accounting": measured})
        if error:
            checkpoint["failures"] = failures
            checkpoint["next_retry"] = retry_deadline
            checkpoint["retry_deadline_basis"] = retry_basis
            blocked.add(key)
        else:
            checkpoint["failures"] = 0
            checkpoint["next_retry"] = 0
        attempts.append({"market": key, **{field: value for field, value in checkpoint.items()
                                                  if field != "http_accounting"}})
        job["updated_at"] = receipt
        _save_job(store, job)
        if hard_limit or used["normalized_page_bytes"] >= request["max_bytes"]:
            break


def history_backfill_status(database, *, mode="import", limit=20):
    """Read bounded mutable job checkpoints without network, recovery or writes."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("History backfill status limit must be from 1 to 100.")
    if mode != "import":
        return {"status": "selection_mismatch", "mode": mode, "required_mode": "import", "jobs": [],
                "total_jobs": 0, "omitted_jobs": 0, "truncated": False}
    path = _path(database)
    with read_database_if_present(path) as con:
        if con is None or "history_backfill_jobs" not in database_table_names(con):
            return {"status": "missing", "mode": mode, "jobs": [], "total_jobs": 0,
                    "omitted_jobs": 0, "truncated": False}
        total = con.execute("SELECT count(*) FROM history_backfill_jobs").fetchone()[0]
        rows = con.execute("SELECT payload FROM history_backfill_jobs ORDER BY updated_at DESC,job_id LIMIT ?", (limit,))
        jobs = [json.loads(row[0]) for row in rows]
    return {"status": "ok", "mode": mode, "jobs": jobs, "total_jobs": total,
            "omitted_jobs": max(0, total - len(jobs)), "truncated": total > len(jobs)}
