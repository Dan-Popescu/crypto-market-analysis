"""Synthetic loopback evidence for actual HTTP admission, deadlines and sharing."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from types import SimpleNamespace

import httpx
import pytest
import requests
from urllib3.util import Retry

from crypto_market_analysis.config import Settings, load_settings
from crypto_market_analysis.http_accounting import (
    accounting_scope,
    http_source_scope,
    instrument_exchange,
    instrument_httpx,
    public_error,
)
from crypto_market_analysis.http_limits import HTTPBudgetExceeded, HTTPPolicy, http_budget_scope
from crypto_market_analysis.providers import PublicHTTP


@contextmanager
def loopback(*, retry_after="0"):
    hits, guard = [], Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with guard:
                hits.append(self.path)
                number = hits.count(self.path)
            if self.path == "/slow":
                time.sleep(0.2)
            status = 302 if self.path == "/redirect" else 429 if self.path in ("/retry", "/long-retry") and number == 1 else 200
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.send_header("Retry-After", retry_after)
            if status == 302:
                self.send_header("Location", "/ok")
            self.end_headers()
            try:
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("requests_limit", [True, 0, -1, 1.1, 1_000_001])
def test_policy_rejects_invalid_request_limits(requests_limit):
    with pytest.raises(ValueError):
        HTTPPolicy(max_requests=requests_limit)


@pytest.mark.parametrize("deadline", [True, 0, -1, float("inf"), float("nan"), "3", 86401])
def test_policy_rejects_nonfinite_or_invalid_deadlines(deadline):
    with pytest.raises(ValueError):
        HTTPPolicy(deadline_seconds=deadline)


def test_settings_validate_http_policy(tmp_path):
    path = tmp_path / "observer.toml"
    path.write_text("http_max_requests = 2\nhttp_deadline_seconds = 0.25\n")
    assert HTTPPolicy.from_settings(load_settings(path)) == HTTPPolicy(2, 0.25)
    assert HTTPPolicy.from_settings(Settings()).max_requests == 10_000


@pytest.mark.parametrize("path", ["/redirect", "/retry"])
def test_requests_retries_and_redirects_refuse_before_second_send(path):
    with loopback() as (root, hits), requests.Session() as session:
        session.mount("http://", requests.adapters.HTTPAdapter(max_retries=Retry(
            total=3, status_forcelist=[429], backoff_factor=0)))
        with http_budget_scope(HTTPPolicy(1, 2)) as budget, accounting_scope("kraken") as meter:
            assert instrument_exchange(SimpleNamespace(session=session), "kraken")
            with pytest.raises(HTTPBudgetExceeded, match="request_limit"):
                session.get(root + path)
            measurement = meter.snapshot("kraken")
    assert hits == [path]
    assert measurement["totals"]["requests"] == budget.used_requests == 1
    assert measurement["totals"]["responses"] == 1
    assert budget.refused_requests == 1
    assert measurement["http_limits"]["enforcement"] == "supported_transports"


def test_requests_successful_hidden_retry_counts_both_attempts():
    with loopback() as (root, hits), requests.Session() as session:
        session.mount("http://", requests.adapters.HTTPAdapter(max_retries=Retry(total=2, status_forcelist=[429])))
        with http_budget_scope(HTTPPolicy(2, 2)) as budget, accounting_scope("kraken") as meter:
            instrument_exchange(SimpleNamespace(session=session), "kraken")
            assert session.get(root + "/retry").status_code == 200
            measurement = meter.snapshot("kraken")
    assert hits == ["/retry", "/retry"]
    assert measurement["totals"]["requests"] == measurement["totals"]["responses"] == budget.used_requests == 2
    assert measurement["totals"]["errors"] == 1


def test_httpx_redirect_and_public_retry_respect_exact_limit():
    for path in ("/redirect", "/retry"):
        with loopback() as (root, hits), httpx.Client(follow_redirects=True) as raw:
            with http_budget_scope(HTTPPolicy(1, 2)) as budget, accounting_scope("deribit") as meter:
                instrument_httpx(raw, "deribit")
                with http_source_scope("deribit"), pytest.raises(HTTPBudgetExceeded):
                    PublicHTTP(client=raw).get_json(root + path)
                measurement = meter.snapshot("deribit")
        assert hits == [path]
        assert measurement["totals"]["requests"] == budget.used_requests == 1
        assert measurement["totals"]["responses"] == 1


def test_httpx_internal_connection_retries_are_bounded_and_counted():
    # Reserve a local port, then close the listener: no external address is contacted.
    with loopback() as (root, _hits):
        pass
    with httpx.Client(transport=httpx.HTTPTransport(retries=3)) as raw:
        with http_budget_scope(HTTPPolicy(2, 2)) as budget, accounting_scope("deribit") as meter:
            instrument_httpx(raw, "deribit")
            with http_source_scope("deribit"), pytest.raises(HTTPBudgetExceeded):
                raw.get(root)
            measurement = meter.snapshot("deribit")
    assert measurement["totals"]["requests"] == budget.used_requests == 2
    assert measurement["totals"]["errors"] == 2
    assert budget.refused_requests == 1
    assert measurement["http_limits"]["enforcement"] == "supported_transports"


@pytest.mark.parametrize("transport", ["requests", "httpx"])
def test_deadline_clamps_actual_client_timeout_without_mutating_client(transport):
    with loopback() as (root, hits), http_budget_scope(HTTPPolicy(5, 0.05)) as budget:
        with accounting_scope("kraken") as meter:
            started = time.monotonic()
            if transport == "requests":
                with requests.Session() as raw:
                    instrument_exchange(SimpleNamespace(session=raw), "kraken")
                    with pytest.raises(HTTPBudgetExceeded, match="deadline"):
                        raw.get(root + "/slow", timeout=10)
            else:
                with httpx.Client(timeout=10) as raw:
                    instrument_httpx(raw, "kraken")
                    with http_source_scope("kraken"), pytest.raises(HTTPBudgetExceeded, match="deadline"):
                        raw.get(root + "/slow")
                    assert raw.timeout.read == 10
            elapsed = time.monotonic() - started
            with pytest.raises(HTTPBudgetExceeded, match="deadline"):
                budget.check()
            measurement = meter.snapshot("kraken")
    assert elapsed < 0.18
    assert hits == ["/slow"]
    assert measurement["totals"]["requests"] == budget.used_requests == 1
    assert measurement["totals"]["errors"] == 1


def test_threads_share_allowance_but_keep_independent_source_meters():
    with loopback() as (root, hits), http_budget_scope(HTTPPolicy(5, 5)) as budget:
        def worker(source):
            with requests.Session() as raw, accounting_scope(source) as meter:
                instrument_exchange(SimpleNamespace(session=raw), source)
                for _ in range(4):
                    try:
                        raw.get(root)
                    except HTTPBudgetExceeded:
                        break
                return meter.snapshot(source)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(copy_context().run, worker, f"source-{i}") for i in range(4)]
            measurements = [future.result() for future in futures]
    assert len(hits) == budget.used_requests == sum(m["totals"]["requests"] for m in measurements) == 5
    assert budget.refused_requests >= 1


def test_nested_scopes_do_not_reset_outer_limits_and_unknown_is_explicit():
    with http_budget_scope(HTTPPolicy(1, 1)) as outer:
        with http_budget_scope(HTTPPolicy(100, 100)) as inner, accounting_scope("injected") as meter:
            assert inner is outer
            assert not instrument_exchange(object(), "injected")
            inner.admit()
            with pytest.raises(HTTPBudgetExceeded) as exc:
                inner.admit()
            assert public_error(exc.value, include_message=False) == "HTTPBudgetExceeded: request_limit"
            measurement = meter.snapshot("injected")
    assert measurement["totals"] is None
    assert measurement["http_limits"]["enforcement"] == "unsupported"
    assert "http://" not in json.dumps(measurement)


def test_collection_workers_share_budget_and_retain_partial_candles(tmp_path):
    from test_http_accounting import LocalCCXT, local_server

    from crypto_market_analysis.collectors import ExchangeClients
    from crypto_market_analysis.pipeline import collect
    from crypto_market_analysis.storage import Store

    class RecentLocal(LocalCCXT):
        def fetch_ohlcv(self, *args, **kwargs):
            rows = super().fetch_ohlcv(*args, **kwargs)
            for row in rows:
                row[0] = (int(time.time()) // 3600 - 2) * 3_600_000
            return rows

    settings = Settings(assets=["BTC"], venues=["kraken", "coinbase"], quote_currencies=["USD"],
                        workers=2, http_max_requests=3, http_deadline_seconds=5, options=False, context=False)
    with local_server() as (root, hits), ExchangeClients() as clients:
        clients._clients = {venue: RecentLocal(root) for venue in settings.venues}
        report = collect(settings, Store(tmp_path / "collection.sqlite"), tasks=("market",), clients=clients)
    assert len(hits) == report["http_limits"]["used_requests"] == 3
    assert sum(row["http_accounting"]["totals"]["requests"] for row in report["reports"]) == 3
    assert sum(row["inserted_candles"] for row in report["reports"]) >= 1
    assert "HTTPBudgetExceeded" in json.dumps(report)


def test_requests_connection_failures_are_actual_attempts():
    with loopback() as (root, _hits):
        pass
    with requests.Session() as raw, http_budget_scope(HTTPPolicy(2, 2)) as budget:
        raw.mount("http://", requests.adapters.HTTPAdapter(max_retries=Retry(total=5)))
        with accounting_scope("kraken") as meter:
            instrument_exchange(SimpleNamespace(session=raw), "kraken")
            with pytest.raises(HTTPBudgetExceeded):
                raw.get(root)
            measurement = meter.snapshot("kraken")
    assert measurement["totals"]["requests"] == measurement["totals"]["errors"] == budget.used_requests == 2
    assert measurement["totals"]["responses"] == 0


def test_shared_httpx_client_does_not_leak_deadlines_between_threads():
    with loopback() as (root, hits), httpx.Client(timeout=10) as raw:
        def worker(deadline):
            with http_budget_scope(HTTPPolicy(3, deadline)), accounting_scope("kraken"):
                instrument_httpx(raw, "kraken")
                with http_source_scope("kraken"):
                    try:
                        return raw.get(root + "/slow").status_code
                    except HTTPBudgetExceeded:
                        return "deadline"

        with ThreadPoolExecutor(max_workers=2) as pool:
            short = pool.submit(worker, 0.05)
            long = pool.submit(worker, 1)
            assert short.result() == "deadline"
            assert long.result() == 200
        assert raw.timeout.read == 10
    assert hits == ["/slow", "/slow"]


def route_to_loopback(monkeypatch, raw, root):
    """Keep official URL attribution; rewrite only this client's actual destination."""
    from urllib.parse import urlsplit

    get = raw.get

    def local_get(url, **kwargs):
        return get(root + urlsplit(url).path, **kwargs)

    monkeypatch.setattr(raw, "get", local_get)


@pytest.mark.parametrize("url,source", [
    ("https://www.bls.gov/schedule/news_release/bls.ics", "bls"),
    ("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "federalreserve"),
    ("https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html", "ecb"),
])
def test_official_calendar_hosts_have_bounded_public_transport_identity(url, source, monkeypatch):
    with loopback() as (root, hits), httpx.Client() as raw:
        route_to_loopback(monkeypatch, raw, root)
        with http_budget_scope(HTTPPolicy(1, 3)), accounting_scope(source) as meter:
            client = PublicHTTP(client=raw)
            client.get_text(url)
            with pytest.raises(HTTPBudgetExceeded):
                client.get_text(url)
            measurement = meter.snapshot(source)
    assert len(hits) == measurement["totals"]["requests"] == 1
    assert measurement["http_limits"]["enforcement"] == "supported_transports"


def test_engine_calendar_obeys_configured_cap_with_default_public_http(tmp_path, monkeypatch):
    from crypto_market_analysis.engine import Engine

    with loopback() as (root, hits), httpx.Client() as raw:
        route_to_loopback(monkeypatch, raw, root)
        client = PublicHTTP(client=raw)
        monkeypatch.setattr("crypto_market_analysis.providers.PublicHTTP", lambda: client)
        result = Engine(Settings(data_dir=tmp_path, http_max_requests=1)).collect_calendar()
    assert len(hits) == result["http_limits"]["used_requests"] == 1
    assert result["http_limits"]["max_requests"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["source"] == "federalreserve"
    assert result["errors"][0]["error"] == "HTTPBudgetExceeded: request_limit"


def test_engine_book_capture_obeys_configured_cap_including_catalogue(tmp_path):
    from test_http_accounting import LocalCCXT, local_server

    from crypto_market_analysis.collectors import ExchangeClients
    from crypto_market_analysis.engine import Engine

    with local_server() as (root, hits), ExchangeClients() as clients:
        clients._clients["kraken"] = LocalCCXT(root)
        result = Engine(Settings(data_dir=tmp_path, http_max_requests=1)).capture_books(
            sources=["kraken"], assets=["BTC"], quotes=["USD"], clients=clients)
    assert hits == ["/catalogue"]
    assert result["http_limits"]["max_requests"] == result["http_limits"]["used_requests"] == 1
    assert result["inserted_books"] == 0


def test_engine_official_context_shares_configured_budget_across_fx_and_calendar(tmp_path, monkeypatch):
    from crypto_market_analysis.engine import Engine
    from crypto_market_analysis.official_context import OfficialHTTP

    fx = (b'<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
          b'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
          b'<Cube><Cube time="2026-10-28"><Cube currency="USD" rate="1.1234"/>'
          b'</Cube></Cube></gesmes:Envelope>')
    with loopback() as (root, hits):
        client = OfficialHTTP()
        stream = client.client.stream

        def local_stream(method, url, **kwargs):
            return stream(method, root + "/fx", **kwargs)

        monkeypatch.setattr(client.client, "stream", local_stream)
        # Retain the actual standard HTTP response; only replace its synthetic body.
        original = client.get_bytes

        def capture(url):
            original(url)
            return fx

        monkeypatch.setattr(client, "get_bytes", capture)
        try:
            result = Engine(Settings(data_dir=tmp_path, http_max_requests=1)).collect_official_context(
                include=("fx", "calendar"), client=client)
        finally:
            client.close()
    assert len(hits) == result["http_limits"]["used_requests"] == 1
    assert result["inserted"]["fx_reference"] == 1
    assert result["inserted"]["calendar"] == 0
    assert len(result["errors"]) == 1
    assert "HTTPBudgetExceeded" in result["errors"][0]["error"]


def test_local_workflow_capture_shares_configured_cap_across_native_markets_and_books(tmp_path):
    from test_http_accounting import LocalCCXT, local_server

    from crypto_market_analysis.collectors import ExchangeClients
    from crypto_market_analysis.local_workflow import capture_local_inputs, normalize_local_workflow_request
    from crypto_market_analysis.storage import Store

    settings = Settings(data_dir=tmp_path, venues=["kraken"], http_max_requests=2, max_pages=1)
    request = normalize_local_workflow_request(settings, {
        "refresh": True, "sources": ["kraken"], "markets": ["BTC/USD", "ETH/USD"],
        "capture_books": True, "capture_tasks": ["market"],
    })
    with local_server() as (root, hits), ExchangeClients() as clients:
        clients._clients["kraken"] = LocalCCXT(root)
        result = capture_local_inputs(settings, Store(settings.database), request, clients)
    assert hits == ["/catalogue", "/candles"]
    assert result["http_limits"]["used_requests"] == result["http_limits"]["max_requests"] == 2
    assert all(row["http_limits"]["max_requests"] == 2 for row in result["market"]["markets"])
    assert all(row["http_limits"]["used_requests"] == 2 for row in result["books"]["markets"])


def test_public_http_retry_after_wait_ends_at_monotonic_deadline(monkeypatch):
    from crypto_market_analysis.providers import RetryDeferred

    with loopback(retry_after="0.8") as (root, hits), httpx.Client() as raw:
        route_to_loopback(monkeypatch, raw, root)
        client = PublicHTTP(client=raw)
        with http_budget_scope(HTTPPolicy(5, 0.05)), accounting_scope("deribit") as meter:
            started = time.monotonic()
            with pytest.raises(HTTPBudgetExceeded, match="deadline"):
                client.get_json("https://www.deribit.com/long-retry")
            elapsed = time.monotonic() - started
            measurement = meter.snapshot("deribit")
        # The server deadline survives the invocation deadline; it is neither
        # replaced by the shorter wait nor reported as a sent second attempt.
        with pytest.raises(RetryDeferred) as deferred:
            client.get_json("https://www.deribit.com/long-retry")
        assert deferred.value.retry_at > time.time() + 0.3
    assert 0.03 <= elapsed < 0.2
    assert hits == ["/long-retry"]
    assert measurement["totals"]["requests"] == measurement["totals"]["responses"] == 1
    assert measurement["http_limits"]["used_requests"] == 1
    assert measurement["http_limits"]["refused_requests"] == 0
    assert measurement["http_limits"]["exceeded"] == "deadline"
    assert measurement["http_limits"]["remaining_seconds"] == 0


def test_public_http_connection_failure_wait_ends_at_monotonic_deadline(monkeypatch):
    with loopback() as (root, _hits):
        pass
    with httpx.Client() as raw:
        route_to_loopback(monkeypatch, raw, root)
        with http_budget_scope(HTTPPolicy(5, 0.05)), accounting_scope("deribit") as meter:
            started = time.monotonic()
            with pytest.raises(HTTPBudgetExceeded, match="deadline"):
                PublicHTTP(client=raw).get_json("https://www.deribit.com/connection-failure")
            elapsed = time.monotonic() - started
            measurement = meter.snapshot("deribit")
    assert 0.03 <= elapsed < 0.2
    assert measurement["totals"]["requests"] == measurement["totals"]["errors"] == 1
    assert measurement["totals"]["responses"] == 0
    assert measurement["http_limits"]["used_requests"] == 1
    assert measurement["http_limits"]["refused_requests"] == 0
    assert measurement["http_limits"]["exceeded"] == "deadline"
    assert measurement["http_limits"]["remaining_seconds"] == 0


@contextmanager
def delayed_tls_proxy(tmp_path, *, connect_delay=0.15, response_delay=0.25):
    """Actual standard HTTP CONNECT and local TLS, with independent delayed stages."""
    import select
    import socket
    import socketserver
    import ssl
    import subprocess

    cert, key = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-subj", "/CN=localhost", "-days", "1"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            time.sleep(response_delay)
            try:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass

        def log_message(self, *_args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    target.socket = tls.wrap_socket(target.socket, server_side=True)

    class Proxy(socketserver.BaseRequestHandler):
        def handle(self):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
            with socket.create_connection(("127.0.0.1", target.server_port)) as upstream:
                time.sleep(connect_delay)
                try:
                    self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    while True:
                        readable, _, _ = select.select([self.request, upstream], [], [], 1)
                        for incoming in readable:
                            chunk = incoming.recv(65536)
                            if not chunk:
                                return
                            (upstream if incoming is self.request else self.request).sendall(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
    proxy.daemon_threads = True
    threads = [Thread(target=server.serve_forever, daemon=True) for server in (target, proxy)]
    for thread in threads:
        thread.start()
    try:
        yield (f"https://127.0.0.1:{target.server_port}/", f"http://127.0.0.1:{proxy.server_address[1]}", hits)
    finally:
        for server in (proxy, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_standard_httpx_proxy_tls_reclamps_timeout_after_connect_stage(tmp_path):
    with delayed_tls_proxy(tmp_path) as (url, proxy, hits):
        with httpx.Client(proxy=proxy, verify=False, timeout=10) as raw:
            with http_budget_scope(HTTPPolicy(5, 0.25)), accounting_scope("deribit") as meter:
                instrument_httpx(raw, "deribit")
                started = time.monotonic()
                with http_source_scope("deribit"), pytest.raises(HTTPBudgetExceeded, match="deadline"):
                    raw.get(url)
                elapsed = time.monotonic() - started
                measurement = meter.snapshot("deribit")
            assert raw.timeout.read == 10
    assert 0.2 <= elapsed < 0.36
    assert hits == ["/"]
    assert measurement["http_limits"]["enforcement"] == "supported_transports"
    assert measurement["http_limits"]["exceeded"] == "deadline"
    assert measurement["http_limits"]["remaining_seconds"] == 0
    assert measurement["totals"]["requests"] == measurement["totals"]["errors"] == 1


def test_httpx_response_completion_checks_deadline_after_trickled_body():
    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            time.sleep(0.03)
            yield b"{}"

    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=SlowBody()))) as raw:
        with http_budget_scope(HTTPPolicy(5, 0.01)), accounting_scope("deribit") as meter:
            with pytest.raises(HTTPBudgetExceeded, match="deadline"):
                PublicHTTP(client=raw).get_json("https://www.deribit.com/completion")
            measurement = meter.snapshot("deribit")
    assert measurement["http_limits"]["enforcement"] == "unsupported"
    assert measurement["http_limits"]["exceeded"] == "deadline"
    assert measurement["totals"]["responses"] == measurement["totals"]["requests"] == 1
