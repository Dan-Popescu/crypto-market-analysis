"""Bounded public transport accounting, retained with collection run payloads.

Counts transport attempts (including failed connections), never logical adapter
calls. Body bytes are decoded application bytes; HTTP framing, compressed/wire
bytes, request bytes and TLS overhead remain unknown. No URLs or bodies survive
an observation. Endpoint IDs hash only the path, excluding origin/query/userinfo.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

import httpx
import requests

MAX_ENDPOINTS = 32
MAX_READ_RUNS = 10_000
_FIELDS = ("requests", "responses", "errors", "http_errors", "decoded_body_bytes",
           "unknown_body_responses", "elapsed_ms")
_current: ContextVar[HTTPAccounting | None] = ContextVar("public_http_accounting", default=None)
_httpx_attempt: ContextVar[_Attempt | None] = ContextVar("public_httpx_attempt", default=None)
_install_lock = RLock()


def _empty() -> dict:
    return {key: 0 for key in _FIELDS}


def _endpoint(method: str, url: str) -> str:
    # Neither URLs nor path fragments enter persisted state, even on errors.
    digest = hashlib.sha256(urlsplit(str(url)).path.encode()).hexdigest()[:20]
    return f"{method.upper() if method.upper() in ('GET', 'POST', 'HEAD', 'OPTIONS') else 'OTHER'}:path-sha256:{digest}"


@dataclass
class _Attempt:
    meter: HTTPAccounting
    source: str
    endpoint: str
    started: float
    finished: bool = False


class HTTPAccounting:
    """Thread-safe counters; each collection scope owns its own meter.

    Snapshots are detached values; delta subtracts a previous snapshot. Shared
    transport callbacks consult the current context, so another task's requests
    cannot appear in this task's totals. Overflow keeps totals but drops endpoint
    attribution. There is no per-request event list or growing URL registry.
    """

    def __init__(self, endpoint_limit: int = MAX_ENDPOINTS):
        if type(endpoint_limit) is not int or not 1 <= endpoint_limit <= MAX_ENDPOINTS:
            raise ValueError(f"endpoint_limit must be between 1 and {MAX_ENDPOINTS}.")
        self.endpoint_limit = endpoint_limit
        self._lock = RLock()
        self._sources: dict[str, dict] = {}

    def _source(self, source: str) -> dict:
        # Sources are producer identities, never obtained from HTTP input.
        return self._sources.setdefault(source, {"totals": _empty(), "endpoints": {},
                                                "omitted_endpoint_requests": 0,
                                                "instrumented": False, "uninstrumented": False})

    def coverage(self, source: str, *, instrumented: bool) -> None:
        with self._lock:
            self._source(source)["instrumented" if instrumented else "uninstrumented"] = True

    def begin(self, source: str, method: str, url: str) -> _Attempt:
        endpoint = _endpoint(method, url)
        with self._lock:
            state = self._source(source)
            state["instrumented"] = True
            endpoints = state["endpoints"]
            if endpoint not in endpoints and len(endpoints) >= self.endpoint_limit:
                endpoint = "omitted"
                state["omitted_endpoint_requests"] += 1
            elif endpoint not in endpoints:
                endpoints[endpoint] = _empty()
            state["totals"]["requests"] += 1
            if endpoint != "omitted":
                endpoints[endpoint]["requests"] += 1
        return _Attempt(self, source, endpoint, time.monotonic())

    def finish(self, attempt: _Attempt, *, status: int | None = None,
               decoded_bytes: int | None = None, error: bool = False) -> None:
        with self._lock:
            if attempt.finished:
                return
            attempt.finished = True
            state = self._source(attempt.source)
            values = {"responses": int(status is not None),
                      "errors": int(error or (status is not None and status >= 400)),
                      "http_errors": int(status is not None and status >= 400),
                      "decoded_body_bytes": decoded_bytes or 0,
                      "unknown_body_responses": int(status is not None and decoded_bytes is None),
                      "elapsed_ms": max(0, (time.monotonic() - attempt.started) * 1000)}
            for target in (state["totals"], state["endpoints"].get(attempt.endpoint)):
                if target is not None:
                    for key, value in values.items():
                        target[key] += value

    def snapshot(self, source: str) -> dict:
        with self._lock:
            state = self._source(source)
            coverage = ("partial" if state["uninstrumented"] and state["instrumented"] else
                        "observed" if state["instrumented"] else "uninstrumented")
            return {"version": 1, "coverage": coverage,
                    "totals": dict(state["totals"]) if state["instrumented"] else None,
                    "endpoints": [{"endpoint": endpoint, **values}
                                  for endpoint, values in sorted(state["endpoints"].items())],
                    "omitted_endpoint_requests": state["omitted_endpoint_requests"],
                    "endpoints_truncated": state["omitted_endpoint_requests"] > 0,
                    "body_byte_basis": "decoded_response_body", "wire_bytes": None,
                    "request_bytes": None, "tls_bytes": None,
                    "request_basis": "transport_attempts_including_connection_failures"}

    def delta(self, source: str, before: dict) -> dict:
        after = self.snapshot(source)
        if before.get("totals") is not None and after["totals"] is not None:
            after["totals"] = {key: after["totals"][key] - before["totals"][key] for key in _FIELDS}
        prior = {row["endpoint"]: row for row in before.get("endpoints", [])}
        after["endpoints"] = [{"endpoint": row["endpoint"], **{
            key: row[key] - prior.get(row["endpoint"], {}).get(key, 0) for key in _FIELDS}}
            for row in after["endpoints"]]
        after["omitted_endpoint_requests"] -= before.get("omitted_endpoint_requests", 0)
        return after


def account_collection(function):
    """Attach a detached per-source scope measurement to CollectionResults."""
    @wraps(function)
    def measured(*args, **kwargs):
        if _current.get() is not None:
            return function(*args, **kwargs)
        meter = HTTPAccounting()
        token = _current.set(meter)
        try:
            result = function(*args, **kwargs)
            for row in result if isinstance(result, list) else [result]:
                row.http_accounting = meter.snapshot(row.source)
            return result
        finally:
            _current.reset(token)
    return measured


def instrument_exchange(client, source: str) -> bool:
    """Observe requests adapter sends used by synchronous CCXT, including catalogue.

    CCXT's default requests adapters have no urllib3 retries. A caller enabling
    hidden urllib3 retries gets partial coverage: those inner attempts are unknown.
    """
    session = getattr(client, "session", None)
    valid = isinstance(session, requests.Session)
    meter = _current.get()
    if meter:
        meter.coverage(source, instrumented=valid)
    if not valid:
        return False
    with _install_lock:
        for adapter in session.adapters.values():
            if not isinstance(adapter, requests.adapters.HTTPAdapter):
                if meter:
                    meter.coverage(source, instrumented=False)
                continue
            if getattr(adapter.max_retries, "total", None) not in (0, False):
                if meter:
                    meter.coverage(source, instrumented=False)
            if getattr(adapter, "_observer_accounted", False):
                continue
            send = adapter.send

            def observed_send(request, *args, _send=send, **kwargs):
                current = _current.get()
                if current is None:
                    return _send(request, *args, **kwargs)
                attempt = current.begin(source, request.method, request.url)
                response = None
                try:
                    response = _send(request, *args, **kwargs)
                    body = response.content
                    current.finish(attempt, status=response.status_code, decoded_bytes=len(body))
                    return response
                except Exception:
                    current.finish(attempt, status=response.status_code if response is not None else None,
                                   error=True)
                    raise

            adapter.send = observed_send
            adapter._observer_accounted = True
    return True


def _provider_source(url: str) -> str | None:
    return {"www.deribit.com": "deribit", "community-api.coinmetrics.io": "coinmetrics",
            "stablecoins.llama.fi": "defillama", "fred.stlouisfed.org": "fred"}.get(urlsplit(str(url)).hostname)


def instrument_httpx(client, source: str) -> bool:
    """Install public HTTPX lifecycle hooks; response.read measures decoded bytes.

    Request hooks run for every redirect hop. httpx_get records transport
    failures; response hooks run for HTTP errors as well as successes.
    """
    valid = isinstance(client, httpx.Client)
    meter = _current.get()
    if meter:
        meter.coverage(source, instrumented=valid)
    if not valid:
        return False
    with _install_lock:
        if getattr(client, "_observer_accounted", False):
            return True

        def request_hook(request):
            current = _current.get()
            # Producer source is attached transiently by PublicHTTP, not persisted.
            identity = _httpx_source.get() or _provider_source(str(request.url))
            if current is not None and identity is not None:
                attempt = current.begin(identity, request.method, str(request.url))
                request.extensions["observer_attempt"] = attempt
                _httpx_attempt.set(attempt)

        def response_hook(response):
            attempt = response.request.extensions.pop("observer_attempt", None)
            if attempt is not None:
                try:
                    body = response.read()
                except Exception:
                    attempt.meter.finish(attempt, status=response.status_code, error=True)
                    raise
                attempt.meter.finish(attempt, status=response.status_code, decoded_bytes=len(body))

        client.event_hooks["request"].append(request_hook)
        client.event_hooks["response"].append(response_hook)
        client._observer_accounted = True
    return True


_httpx_source: ContextVar[str | None] = ContextVar("public_httpx_source", default=None)


def prepare_httpx(client, url: str):
    source = _provider_source(url)
    if source:
        instrument_httpx(client, source)


def httpx_get(client, url: str, params: dict | None = None):
    """Keep attempt attribution local even when one HTTPX client spans tasks."""
    source_token = _httpx_source.set(_provider_source(url))
    attempt_token = _httpx_attempt.set(None)
    try:
        prepare_httpx(client, url)
        return client.get(url, params=params)
    except Exception:
        attempt = _httpx_attempt.get()
        if attempt is not None:
            attempt.meter.finish(attempt, error=True)
        raise
    finally:
        _httpx_source.reset(source_token)
        _httpx_attempt.reset(attempt_token)


def mark_uninstrumented(source: str) -> None:
    if (meter := _current.get()) is not None:
        meter.coverage(source, instrumented=False)


def merge_measurements(measurements: list[dict | None], *, endpoint_limit: int = 100) -> dict:
    """Bounded aggregate; unknown measurements never become zero HTTP work."""
    if type(endpoint_limit) is not int or not 1 <= endpoint_limit <= 500:
        raise ValueError("endpoint_limit must be between 1 and 500.")
    totals = _empty()
    endpoints = {}
    omitted = 0
    measured = unknown = partial = 0
    for measurement in measurements:
        if not measurement or measurement.get("totals") is None:
            unknown += 1
            continue
        measured += 1
        partial += measurement.get("coverage") == "partial"
        for key in _FIELDS:
            totals[key] += measurement["totals"].get(key, 0)
        omitted += measurement.get("omitted_endpoint_requests", 0)
        for row in measurement.get("endpoints", []):
            endpoint = row["endpoint"]
            if endpoint not in endpoints and len(endpoints) >= endpoint_limit:
                omitted += row["requests"]
                continue
            target = endpoints.setdefault(endpoint, _empty())
            for key in _FIELDS:
                target[key] += row.get(key, 0)
    coverage = ("partial" if unknown or partial else "observed") if measured else (
        "uninstrumented" if unknown else "unknown")
    return {"version": 1, "coverage": coverage,
            "totals": totals if measured else None,
            "endpoints": [{"endpoint": endpoint, **values} for endpoint, values in sorted(endpoints.items())],
            "observed_runs": measured, "uninstrumented_runs": unknown, "partial_runs": partial,
            "omitted_endpoint_requests": omitted, "endpoints_truncated": omitted > 0,
            "body_byte_basis": "decoded_response_body",
            "wire_bytes": None, "request_bytes": None, "tls_bytes": None}


def stored_http_accounting(store_or_path, *, source: str | None = None, task: str | None = None,
                           mode: str = "live", start: int | None = None, end: int | None = None,
                           limit: int = 1000, endpoint_limit: int = 100) -> dict:
    """Read retained run history only; source/mode/time filters precede limits.

    This creates no schema, caches, or files. Omissions are explicit. Unknown
    legacy mode is selectable as 'unknown', never silently classified as live.
    Existing run retention/archives govern availability; this is no lifetime bill.
    """
    from .storage import Store

    if mode not in ("live", "demo", "import", "unknown", "all"):
        raise ValueError("mode must be live, demo, import, unknown or all.")
    if type(limit) is not int or not 1 <= limit <= MAX_READ_RUNS:
        raise ValueError(f"limit must be between 1 and {MAX_READ_RUNS}.")
    if type(endpoint_limit) is not int or not 1 <= endpoint_limit <= 500:
        raise ValueError("endpoint_limit must be between 1 and 500.")
    for name, value in (("source", source), ("task", task)):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable()):
            raise ValueError(f"{name} must be a bounded printable identifier.")
    cutoff = int(time.time()) if end is None else end
    for value in (start, cutoff):
        if value is not None and (type(value) is not int or not 0 <= value <= 253402300799):
            raise ValueError("Time filters require nonnegative integer UTC seconds.")
    if start is not None and start > cutoff:
        raise ValueError("start must be <= end.")
    path = store_or_path if isinstance(store_or_path, (Path, str)) else store_or_path.path
    # A writable configured Store must still use an actual read-only connection.
    store = Store(Path(path), initialize=False)
    selection = {"source": source, "task": task, "mode": mode, "start": start, "end": cutoff,
                 "limit": limit, "endpoint_limit": endpoint_limit}

    def unavailable(status: str, reason: str) -> dict:
        return {"status": status, "reason": reason, "selection": selection,
                "matched_runs": None, "included_runs": 0, "omitted_runs": None, "truncated": False,
                "summary": merge_measurements([], endpoint_limit=endpoint_limit), "sources": [],
                "availability": "unknown"}

    if not store.path.is_file():
        return unavailable("missing", "Configured database is missing; no measurements were read.")
    conditions, params = ["observed_at<=?"], [cutoff]
    if start is not None:
        conditions.append("observed_at>=?")
        params.append(start)
    for key, value in (("source", source), ("task", task)):
        if value is not None:
            conditions.append(f"{key}=?")
            params.append(value)
    if mode == "unknown":
        conditions.append("json_extract(payload,'$.data_mode') IS NULL")
    elif mode != "all":
        conditions.append("json_extract(payload,'$.data_mode')=?")
        params.append(mode)
    where = " AND ".join(conditions)
    try:
        with store.connection() as con:
            con.execute("BEGIN")
            columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
            if not {"id", "source", "task", "observed_at", "payload"} <= columns:
                return unavailable("unavailable", "Retained run accounting is unavailable in this database schema.")
            matched = con.execute(f"SELECT COUNT(*) FROM runs WHERE {where}", params).fetchone()[0]
            rows = con.execute(f"SELECT source,task,observed_at,payload FROM runs WHERE {where} "
                               "ORDER BY observed_at DESC,id DESC LIMIT ?", [*params, limit]).fetchall()
    except ValueError:
        # The read-only Store wraps database errors. Keep path/error details out
        # of the public accounting response; unavailable work remains unknown.
        return unavailable("unavailable", "Configured database could not be read without initialization.")
    measurements = [json.loads(row[3]).get("http_accounting") for row in rows]
    grouped = {}
    for row, measurement in zip(rows, measurements, strict=True):
        grouped.setdefault((row[0], row[1]), []).append(measurement)
    return {"status": "ok" if rows else "empty", "selection": selection,
            "matched_runs": matched, "included_runs": len(rows), "omitted_runs": max(0, matched - len(rows)),
            "truncated": matched > len(rows),
            "summary": merge_measurements(measurements, endpoint_limit=endpoint_limit),
            "sources": [{"source": identity, "task": selected_task,
                         **merge_measurements(values, endpoint_limit=endpoint_limit)}
                        for (identity, selected_task), values in sorted(grouped.items())],
            "availability": "retained_runs_only; archived_or_deleted_runs_not_included"}
