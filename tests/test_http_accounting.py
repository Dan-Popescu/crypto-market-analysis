"""Transport-level evidence uses loopback only; no public provider requests."""

import gzip
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Lock, Thread
from urllib.parse import urlsplit

import ccxt
import httpx

from crypto_market_analysis.collectors import ExchangeClients, collect_exchange
from crypto_market_analysis.http_accounting import (
    HTTPAccounting,
    account_collection,
    merge_measurements,
    stored_http_accounting,
)
from crypto_market_analysis.models import CollectionResult
from crypto_market_analysis.providers import PublicHTTP, collect_context, collect_options
from crypto_market_analysis.storage import Store


@contextmanager
def local_server():
    hits = []
    guard = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            with guard:
                hits.append(path)
                count = hits.count(path)
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/gzip")
                self.end_headers()
                return
            status = 429 if path == "/retry" and count == 1 else 200
            payload = [[0, 10, 12, 9, 11, 1]] if path == "/candles" else {"result": [], "message": "synthetic"}
            body = json.dumps(payload).encode()
            if path == "/gzip":
                body = gzip.compress(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if path == "/gzip":
                self.send_header("Content-Encoding", "gzip")
            if status == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(body)

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


class LocalCCXT(ccxt.kraken):
    """Real CCXT HTTP path with a tiny synthetic catalogue on loopback."""

    def __init__(self, root):
        super().__init__({"enableRateLimit": False})
        self.root = root
        self.has = {"fetchOHLCV": True}

    def load_markets(self, reload=False, params=None):
        self.fetch(self.root + "/catalogue?api_key=secret-query")
        return {"spot": {"base": "BTC", "quote": "USD", "spot": True, "symbol": "BTC/USD"}}

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None, params=None):
        return self.fetch(self.root + "/candles?token=secret-query")


def test_real_ccxt_catalogue_and_shared_pool_count_only_each_tasks_work():
    with local_server() as (root, hits), ExchangeClients() as clients:
        client = LocalCCXT(root)
        clients._clients["kraken"] = client
        first = collect_exchange("kraken", ["BTC"], now=7200, include=("candles",), clients=clients)
        second = collect_exchange("kraken", ["BTC"], now=7200, include=("candles",), clients=clients)
    assert hits == ["/catalogue", "/candles", "/candles"]
    assert len(first.candles) == len(second.candles) == 1
    assert first.http_accounting["totals"]["requests"] == 2
    assert second.http_accounting["totals"]["requests"] == 1
    assert first.http_accounting["totals"]["responses"] == 2
    assert first.http_accounting["totals"]["decoded_body_bytes"] > 0
    assert first.http_accounting["totals"]["elapsed_ms"] > 0
    assert first.http_accounting["coverage"] == "observed"
    encoded = json.dumps(first.http_accounting)
    assert all(value not in encoded for value in (root, "secret-query", "catalogue", "api_key", "token="))
    assert first.http_accounting["wire_bytes"] is None


def test_real_ccxt_catalogue_failure_has_transport_accounting():
    class CatalogueFailure(LocalCCXT):
        def load_markets(self, **kwargs):
            self.fetch(self.root + "/catalogue")
            raise ValueError("synthetic invalid catalogue")

    with local_server() as (root, hits), ExchangeClients() as clients:
        clients._clients["kraken"] = CatalogueFailure(root)
        result = collect_exchange("kraken", ["BTC"], include=("candles",), clients=clients)
    assert hits == ["/catalogue"]
    assert result.errors and not result.candles
    assert result.http_accounting["totals"]["requests"] == 1


class LoopbackTransport(httpx.BaseTransport):
    """Keep provider URL hooks intact; send the actual test HTTP to loopback."""

    def __init__(self, root, paths):
        self.root = root
        self.paths = iter(paths)
        self.transport = httpx.HTTPTransport()

    def handle_request(self, request):
        path = next(self.paths)
        local = httpx.Request(request.method, self.root + path)
        return self.transport.handle_request(local)

    def close(self):
        self.transport.close()


def test_httpx_real_local_transport_counts_retry_redirect_and_decoded_body():
    @account_collection
    def collect(client):
        client.get_json("https://www.deribit.com/retry?access_token=secret-query")
        client.get_json("https://www.deribit.com/redirect?access_token=secret-query")
        return CollectionResult("deribit")

    with local_server() as (root, hits):
        with httpx.Client(transport=LoopbackTransport(root, ["/retry", "/retry", "/redirect", "/gzip"]),
                          follow_redirects=True) as transport:
            client = PublicHTTP(client=transport)
            result = collect(client)
    metrics = result.http_accounting
    decoded = len(json.dumps({"result": [], "message": "synthetic"}).encode())
    assert hits == ["/retry", "/retry", "/redirect", "/gzip"]
    assert metrics["totals"]["requests"] == metrics["totals"]["responses"] == 4
    assert metrics["totals"]["http_errors"] == 1
    assert metrics["totals"]["errors"] == 1
    assert metrics["totals"]["decoded_body_bytes"] == decoded * 3
    assert metrics["totals"]["unknown_body_responses"] == 0
    assert "secret-query" not in json.dumps(metrics)


def test_httpx_transport_failure_retry_and_catalogue_requests(monkeypatch):
    monkeypatch.setattr("crypto_market_analysis.providers.time.sleep", lambda _delay: None)
    hits = []

    def handle(request):
        hits.append(request.url.path)
        if len(hits) == 1:
            raise httpx.ConnectError("synthetic secret-query", request=request)
        return httpx.Response(200, json={"result": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        result = collect_options(["BTC"], now=100, client=PublicHTTP(client=transport))
    assert len(hits) == 3  # instrument catalogue retries + summary, not adapter calls
    assert result.http_accounting["totals"]["requests"] == 3
    assert result.http_accounting["totals"]["responses"] == 2
    assert result.http_accounting["totals"]["errors"] == 1
    assert "secret-query" not in json.dumps(result.http_accounting)


def test_failed_response_body_is_unknown_and_counted(monkeypatch):
    monkeypatch.setattr("crypto_market_analysis.providers.time.sleep", lambda _delay: None)

    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError("synthetic body failure")
            yield b""  # pragma: no cover

    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=BrokenBody()))) as transport:
        result = collect_options(["BTC"], now=100, client=PublicHTTP(client=transport))
    # First catalogue attempt and its retry both reach HTTP, unlike cooldown skips.
    assert result.errors
    assert result.http_accounting["totals"]["requests"] == 2
    assert result.http_accounting["totals"]["unknown_body_responses"] == 2
    assert result.http_accounting["totals"]["errors"] == 2


def test_shared_httpx_simultaneous_sources_have_separate_measurements():
    barrier = Barrier(2)
    seen = set()
    guard = Lock()

    def handle(request):
        host = request.url.host
        with guard:
            first = host not in seen
            seen.add(host)
        if first:
            barrier.wait(timeout=5)
        return httpx.Response(200, json={"result": []} if "deribit" in host else [])

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        client = PublicHTTP(client=transport)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(collect_options, ["BTC"], now=100, client=client)
            b = pool.submit(collect_context, now=100, sources=("defillama",), client=client)
            options, context = a.result(), b.result()[0]
    assert options.http_accounting["totals"]["requests"] == 2
    assert context.http_accounting["totals"]["requests"] == 1
    assert len(options.http_accounting["endpoints"]) == 2
    assert len(context.http_accounting["endpoints"]) == 1


def test_bounded_endpoints_snapshot_delta_and_unknown_clients():
    meter = HTTPAccounting(endpoint_limit=2)
    meter.coverage("kraken", instrumented=True)
    before = meter.snapshot("kraken")
    for i in range(10):
        attempt = meter.begin("kraken", "GET", f"https://username:password@example.com/secret-{i}?key=secret")
        meter.finish(attempt, status=200, decoded_bytes=3)
    after = meter.delta("kraken", before)
    assert after["totals"]["requests"] == 10
    assert after["totals"]["decoded_body_bytes"] == 30
    assert len(after["endpoints"]) == 2
    assert after["omitted_endpoint_requests"] == 8
    assert before["totals"]["requests"] == 0
    assert "secret" not in json.dumps(after)
    unknown = CollectionResult("synthetic").http_accounting
    mixed = merge_measurements([after, unknown])
    assert mixed["coverage"] == "partial" and mixed["uninstrumented_runs"] == 1
    assert mixed["totals"]["requests"] == 10


def test_logical_injected_collectors_are_unknown_not_zero():
    class Fake:
        def get_json(self, *_args):
            return {"result": []}

    result = collect_options(["BTC"], now=100, client=Fake())
    assert result.http_accounting["coverage"] == "uninstrumented"
    assert result.http_accounting["totals"] is None
    aggregate = merge_measurements([result.http_accounting, None])
    assert aggregate["coverage"] == "uninstrumented"
    assert aggregate["totals"] is None


def test_mixed_instrumented_and_unknown_transport_preserves_partial_coverage():
    class FakeHTTP:
        def get(self, url, params=None):
            return httpx.Response(200, json={"result": []}, request=httpx.Request("GET", url))

    @account_collection
    def collect(client):
        client.get_json("https://www.deribit.com/observed")
        client.client = FakeHTTP()
        client.get_json("https://www.deribit.com/unknown")
        return CollectionResult("deribit")

    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"result": []}))) as raw:
        result = collect(PublicHTTP(client=raw))
    assert result.http_accounting["coverage"] == "partial"
    assert result.http_accounting["totals"]["requests"] == 1


def measured_result(source="kraken", count=1):
    meter = HTTPAccounting()
    for _ in range(count):
        attempt = meter.begin(source, "GET", "https://example.com/fixture?password=secret")
        meter.finish(attempt, status=200, decoded_bytes=2)
    return CollectionResult(source, http_accounting=meter.snapshot(source))


def test_restart_reads_stored_modes_sources_and_time_filters_before_cap(tmp_path):
    path = tmp_path / "accounting.sqlite"
    store = Store(path)
    store.record_run(measured_result(count=2), "market", 100, data_mode="live")
    store.record_run(measured_result(count=99), "market", 200, data_mode="demo")
    store.record_run(measured_result("binance", count=99), "market", 200, data_mode="live")
    store.record_run(measured_result(count=99), "liquidity", 200, data_mode="live")
    store.record_run(CollectionResult("kraken"), "market", 50)
    stored = stored_http_accounting(path, source="kraken", task="market", mode="live", start=90, end=200, limit=1)
    assert stored["matched_runs"] == stored["included_runs"] == 1
    assert stored["summary"]["totals"]["requests"] == 2
    assert stored["summary"]["uninstrumented_runs"] == 0
    assert stored["sources"][0]["source"] == "kraken"
    assert stored["omitted_runs"] == 0 and not stored["truncated"]
    legacy = stored_http_accounting(Store(path, initialize=False), mode="unknown", end=200)
    assert legacy["included_runs"] == 1
    assert legacy["summary"]["totals"] is None
    mixed = stored_http_accounting(path, mode="all", end=200, endpoint_limit=1)
    assert mixed["summary"]["coverage"] == "partial"
    assert mixed["summary"]["uninstrumented_runs"] == 1
    assert mixed["summary"]["totals"]["requests"] == 299
    capped = stored_http_accounting(path, mode="all", end=200, limit=1)
    assert capped["matched_runs"] == 5 and capped["included_runs"] == 1
    assert capped["omitted_runs"] == 4 and capped["truncated"]
    assert "secret" not in json.dumps(mixed)


def test_stored_read_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite"
    result = stored_http_accounting(path)
    assert result["status"] == "missing"
    assert result["matched_runs"] is result["summary"]["totals"] is None
    assert not path.exists()


def test_stored_read_old_or_invalid_database_returns_unknown_without_migration(tmp_path):
    old = tmp_path / "old.sqlite"
    with sqlite3.connect(old) as con:
        con.execute("CREATE TABLE unrelated (value TEXT)")
    assert stored_http_accounting(old)["status"] == "unavailable"
    with sqlite3.connect(old) as con:
        assert [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["unrelated"]
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("synthetic invalid database")
    result = stored_http_accounting(invalid)
    assert result["status"] == "unavailable" and result["summary"]["totals"] is None
    empty = stored_http_accounting(Store(tmp_path / "empty.sqlite"))
    assert empty["status"] == "empty" and empty["summary"]["totals"] is None
