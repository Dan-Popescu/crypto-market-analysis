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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

import httpx
import requests
from urllib3.util import Timeout

from .http_limits import HTTPBudgetExceeded, current_http_budget, http_budget_scope

MAX_ENDPOINTS = 32
MAX_READ_RUNS = 10_000
_FIELDS = ("requests", "responses", "errors", "http_errors", "decoded_body_bytes",
           "unknown_body_responses", "elapsed_ms")
_current: ContextVar[HTTPAccounting | None] = ContextVar("public_http_accounting", default=None)
_requests_source: ContextVar[str | None] = ContextVar("public_requests_source", default=None)
_httpx_attempt: ContextVar[_Attempt | None] = ContextVar("public_httpx_attempt", default=None)
_install_lock = RLock()
_requests_attempts: ContextVar[list | None] = ContextVar("public_requests_attempts", default=None)


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
        self.budget = current_http_budget()

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
            # Installing one supported adapter proves neither that an injected
            # client used it nor that other transports did no work. An incomplete
            # scope needs actual measured attempts before it has a known subset.
            observed = state["instrumented"] and (
                not state["uninstrumented"] or state["totals"]["requests"] > 0)
            coverage = ("partial" if state["uninstrumented"] and observed else
                        "observed" if observed else "uninstrumented")
            return {"version": 1, "coverage": coverage,
                    "totals": dict(state["totals"]) if observed else None,
                    "endpoints": [{"endpoint": endpoint, **values}
                                  for endpoint, values in sorted(state["endpoints"].items())],
                    "omitted_endpoint_requests": state["omitted_endpoint_requests"],
                    "endpoints_truncated": state["omitted_endpoint_requests"] > 0,
                    "body_byte_basis": "decoded_response_body", "wire_bytes": None,
                    "request_bytes": None, "tls_bytes": None,
                    "request_basis": "transport_attempts_including_connection_failures",
                    "http_limits": self.budget.snapshot() if self.budget else None}

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


@contextmanager
def accounting_scope(source: str):
    """Measure one explicit operation, including exceptions, with detached totals.

    The caller owns retention and decides whether its adapter is instrumented.
    Nested scopes restore both the outer meter and producer identity.
    """
    with http_budget_scope():
        meter = HTTPAccounting()
        token = _current.set(meter)
        source_token = _requests_source.set(source)
        try:
            yield meter
        finally:
            _requests_source.reset(source_token)
            _current.reset(token)


@contextmanager
def http_source_scope(source: str):
    """Bind a public operation's identity while preserving its shared limiter."""
    requests_token = _requests_source.set(source)
    httpx_token = _httpx_source.set(source)
    try:
        yield
    finally:
        _httpx_source.reset(httpx_token)
        _requests_source.reset(requests_token)


def account_collection(function):
    """Attach a detached per-source scope measurement to CollectionResults."""
    @wraps(function)
    def measured(*args, **kwargs):
        if _current.get() is not None:
            return function(*args, **kwargs)
        with http_budget_scope():
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


def account_exchange(function):
    """Bind the producer to this call, independent of shared sessions/adapters.

    A nested exchange call restores its caller's source, including after an
    exception. Other threads keep their own source and accounting scope.
    """
    @account_collection
    @wraps(function)
    def measured(venue, *args, **kwargs):
        token = _requests_source.set(venue)
        try:
            return function(venue, *args, **kwargs)
        finally:
            _requests_source.reset(token)
    return measured


def _instrument_pool(pool):
    """Gate urllib3's actual attempts, below redirect and retry loops."""
    with _install_lock:
        if getattr(pool, "_observer_accounted", False):
            return
        send = pool._make_request

        def measured(conn, method, url, *args, **kwargs):
            current, identity = _current.get(), _requests_source.get()
            pending = _requests_attempts.get()
            budget = current_http_budget()
            if identity is None or (current is None and budget is None):
                return send(conn, method, url, *args, **kwargs)
            # A preceding successful raw response was consumed by urllib3 for a
            # retry. Its decoded body size is unknown, rather than counted twice.
            if pending:
                previous, status = pending[-1]
                if previous is not None:
                    previous.meter.finish(previous, status=status)
            if budget:
                remaining = budget.admit()
                original = kwargs.get("timeout")
                timeout = pool._get_timeout(original)
                connect = timeout.connect_timeout
                read = timeout.read_timeout
                kwargs["timeout"] = Timeout(
                    total=remaining, connect=min(connect, remaining) if connect is not None else remaining,
                    read=min(read, remaining) if read is not None else remaining)
            attempt = current.begin(identity, method, url) if current else None
            try:
                response = send(conn, method, url, *args, **kwargs)
            except Exception:
                if attempt:
                    current.finish(attempt, error=True)
                if budget and budget.remaining() <= 0:
                    budget.check()
                raise
            if pending is not None:
                pending.append((attempt, response.status))
            elif attempt:
                current.finish(attempt, status=response.status)
            return response

        pool._make_request = measured
        pool._observer_accounted = True


def instrument_exchange(client, source: str) -> bool:
    """Instrument standard Requests adapters, including urllib3 retries.

    Custom adapters are left explicit as unsupported; shared wrappers consult
    context at call time and never mutate session/client timeout settings.
    """
    session = getattr(client, "session", None)
    meter, budget = _current.get(), current_http_budget()
    if not isinstance(session, requests.Session):
        if meter:
            meter.coverage(source, instrumented=False)
        if budget:
            budget.unsupported()
        return False
    supported = True
    with _install_lock:
        for adapter in session.adapters.values():
            valid = type(adapter) is requests.adapters.HTTPAdapter
            if meter:
                meter.coverage(source, instrumented=valid)
            if valid and budget:
                budget.supported()
            if not valid:
                supported = False
                if budget:
                    budget.unsupported()
                continue
            if getattr(adapter, "_observer_accounted", False):
                continue
            # Both Requests interfaces are handled for supported installed versions.
            for name in ("get_connection", "get_connection_with_tls_context"):
                original = getattr(adapter, name, None)
                if original is None:
                    continue

                def connection(*args, _original=original, **kwargs):
                    pool = _original(*args, **kwargs)
                    _instrument_pool(pool)
                    return pool

                setattr(adapter, name, connection)
            send = adapter.send

            def observed_send(request, *args, _send=send, **kwargs):
                pending = []
                token = _requests_attempts.set(pending)
                response = None
                try:
                    response = _send(request, *args, **kwargs)
                    body = response.content
                    if pending and pending[-1][0] is not None:
                        attempt, status = pending[-1]
                        attempt.meter.finish(attempt, status=status, decoded_bytes=len(body))
                    return response
                except Exception:
                    if pending and pending[-1][0] is not None:
                        attempt, status = pending[-1]
                        attempt.meter.finish(attempt, status=status, error=True)
                    if (budget := current_http_budget()) is not None and budget.remaining() <= 0:
                        budget.check()
                    raise
                finally:
                    _requests_attempts.reset(token)

            adapter.send = observed_send
            adapter._observer_accounted = True
    return supported


def _provider_source(url: str) -> str | None:
    return {"www.deribit.com": "deribit", "community-api.coinmetrics.io": "coinmetrics",
            "stablecoins.llama.fi": "defillama", "fred.stlouisfed.org": "fred",
            "www.bls.gov": "bls", "www.federalreserve.gov": "federalreserve",
            "www.ecb.europa.eu": "ecb"}.get(urlsplit(str(url)).hostname)


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
        if (budget := current_http_budget()) is not None:
            budget.unsupported()
        return False
    transports = [client._transport, *[t for t in client._mounts.values() if t is not None]]
    supported = all(type(t) is httpx.HTTPTransport for t in transports)
    if (budget := current_http_budget()) is not None:
        if supported:
            budget.supported()
        else:
            budget.unsupported()
    if not supported and meter:
        meter.coverage(source, instrumented=False)
    with _install_lock:
        if getattr(client, "_observer_accounted", False):
            return True

        def request_hook(request):
            current = _current.get()
            # Producer source is attached transiently by PublicHTTP, not persisted.
            identity = _httpx_source.get() or _provider_source(str(request.url))
            if identity is not None and (budget := current_http_budget()) is not None:
                remaining = budget.admit()
                request.extensions["timeout"] = {
                    key: min(value, remaining) if value is not None else remaining
                    for key, value in request.extensions.get("timeout", {
                        "connect": None, "read": None, "write": None, "pool": None}).items()}
            if current is not None and identity is not None:
                attempt = current.begin(identity, request.method, str(request.url))
                request.extensions["observer_attempt"] = attempt
                _httpx_attempt.set(attempt)
            if identity is not None and type(client._transport_for_url(request.url)) is httpx.HTTPTransport:
                previous_trace = request.extensions.get("trace")
                if getattr(previous_trace, "_observer_trace", False):
                    previous_trace = previous_trace._original
                connects = 0

                def trace(event, info):
                    nonlocal connects
                    budget = current_http_budget()
                    connecting = event.endswith(("connect_tcp.started", "connect_unix_socket.started"))
                    if connecting:
                        if connects:
                            if budget:
                                budget.admit()
                            if current:
                                retry = current.begin(identity, request.method, str(request.url))
                                request.extensions["observer_attempt"] = retry
                                _httpx_attempt.set(retry)
                        connects += 1
                    if event.endswith(".failed"):
                        if (failed := request.extensions.get("observer_attempt")) is not None:
                            failed.meter.finish(failed, error=True)
                        if budget and budget.remaining() <= 0:
                            budget.check()
                    if budget and event.endswith(".started"):
                        remaining = budget.remaining()
                        if remaining <= 0:
                            budget.check()
                        # HTTP/1.1 and HTTP/2 operations read timeout extensions
                        # after this trace callback; proxy CONNECT may have its
                        # own request object. TLS/connect use scalar kwargs.
                        operation = info.get("request", request)
                        for scoped_request in (request, operation):
                            timeouts = scoped_request.extensions.get("timeout", {})
                            scoped_request.extensions["timeout"] = {
                                key: min(value, remaining) if value is not None else remaining
                                for key, value in timeouts.items()}
                        if "timeout" in info:
                            value = info["timeout"]
                            info["timeout"] = min(value, remaining) if value is not None else remaining
                    if budget and event.endswith(".complete") and budget.remaining() <= 0:
                        if (late := request.extensions.get("observer_attempt")) is not None:
                            received = info.get("return_value")
                            status = (received[1] if event.endswith("receive_response_headers.complete")
                                      and isinstance(received, tuple) and len(received) > 1 else None)
                            late.meter.finish(late, status=status, error=True)
                        budget.check()
                    if previous_trace:
                        previous_trace(event, info)

                trace._observer_trace = True
                trace._original = previous_trace
                request.extensions["trace"] = trace

        def response_hook(response):
            attempt = response.request.extensions.pop("observer_attempt", None)
            if attempt is not None:
                if response.request.extensions.get("observer_stream"):
                    attempt.meter.finish(attempt, status=response.status_code)
                    return
                try:
                    body = response.read()
                except Exception:
                    attempt.meter.finish(attempt, status=response.status_code, error=True)
                    raise
                attempt.meter.finish(attempt, status=response.status_code, decoded_bytes=len(body))
            if (budget := current_http_budget()) is not None and budget.remaining() <= 0:
                budget.check()

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
    source_token = _httpx_source.set(_provider_source(url) or _httpx_source.get())
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
    if (budget := current_http_budget()) is not None:
        budget.unsupported()
    if (meter := _current.get()) is not None:
        meter.coverage(source, instrumented=False)


def public_error(exc: Exception, *, include_message: bool = True) -> str:
    """Project transport failures without provider URLs, headers or response text.

    Keep local validation errors and RetryDeferred's explicit deadline intact
    by default. Explicit operation journals can suppress arbitrary client text.
    CCXT subclasses retain their scheduler-relevant type without eagerly
    importing the entire CCXT package at application startup.
    """
    kind = type(exc).__name__
    if isinstance(exc, HTTPBudgetExceeded):
        return f"HTTPBudgetExceeded: {exc.reason}"
    if isinstance(exc, (httpx.HTTPStatusError, requests.exceptions.HTTPError)):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return f"{kind}: HTTP {status}" if type(status) is int else kind
    if isinstance(exc, (httpx.HTTPError, requests.exceptions.RequestException)):
        return kind
    if any(parent.__module__.startswith("ccxt.") for parent in type(exc).__mro__):
        return kind
    return f"{kind}: {exc}" if include_message else kind


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
