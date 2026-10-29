"""Named repair accounting uses real loopback transports and synthetic evidence."""

import asyncio
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit

import ccxt
import pytest
import requests

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.history_dataset import (
    DatasetConfig,
    HistoryDataset,
    KrakenRESTCapture,
    RESTCapture,
    UnsupportedMarket,
)
from crypto_market_analysis.http_accounting import accounting_scope, stored_http_accounting
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage_policy import StoragePolicy, maintain_storage, verify_archive

NOW = 200 * 3600
ROWS = [[0, 10, 12, 9, 11, 2]]
RAW = json.dumps(ROWS).encode()


@contextmanager
def transport():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            hits.append(path)
            if path == "/disconnect":
                self.close_connection = True
                return
            status = 503 if path.endswith("fail") else 429 if path == "/retry" and hits.count(path) == 1 else 200
            body = b"secret-body" if status != 200 else RAW if path in ("/candles", "/retry") else b'{}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class LocalKraken(ccxt.kraken):
    """The production CCXT requests path, with only its public endpoints replaced."""

    def __init__(self, root, *, catalogue="/catalogue", candles="/candles", supported=True, retry=False):
        super().__init__({"enableRateLimit": False, "timeout": 1000})
        self.root, self.catalogue, self.candles = root, catalogue, candles
        self.supported, self.retry = supported, retry

    def load_markets(self):
        if not self.markets:
            self.fetch(self.root + self.catalogue + "?token=secret-query")
            self.markets = {"BTC/EUR": {"spot": self.supported}}
        return self.markets

    def fetch_ohlcv(self, *_args, **_kwargs):
        for index in range(2 if self.retry else 1):
            try:
                return self.fetch(self.root + self.candles + "?token=secret-query")
            except ccxt.BaseError:
                if not self.retry or index:
                    raise


def dataset(tmp_path, *, clock=lambda: NOW):
    return HistoryDataset(DatasetConfig("repair-http", str(tmp_path / "named"), 0, 3600), clock=clock)


def retained(ds):
    with ds.store.connection() as con:
        return ([json.loads(row[0]) for row in con.execute("SELECT payload FROM runs ORDER BY id")],
                [json.loads(row[0]) for row in con.execute("SELECT payload FROM history_attempts ORDER BY id")])


def assert_private(runs, attempts, root=None):
    encoded = json.dumps([runs, attempts])
    assert all(value not in encoded for value in ("secret-query", "secret-body", "token=", "https://", "http://"))
    if root:
        assert root not in encoded


def test_catalogue_and_fetch_measured_without_changing_immutable_capture(tmp_path):
    ds = dataset(tmp_path)
    with transport() as (root, hits):
        client = LocalKraken(root)
        try:
            adapter = KrakenRESTCapture(client)
            result = ds.append_rest("BTC", "EUR", adapter, since=0, until=3600, limit=1)
            repeated = ds.append_rest("BTC", "EUR", adapter, since=0, until=3600, limit=1)
        finally:
            client.close()
    assert hits == ["/catalogue", "/candles", "/candles"]
    assert result["http_accounting"]["totals"]["requests"] == 2
    assert result["http_accounting"]["totals"]["responses"] == 2
    assert result["http_accounting"]["totals"]["decoded_body_bytes"] == 2 + len(RAW)
    assert repeated["http_accounting"]["totals"]["requests"] == 1
    assert result["logical_fetch_calls"] == repeated["logical_fetch_calls"] == 1
    assert result["http_accounting_retention"] == "retained"
    assert result["id"] == repeated["id"]
    assert result["http_accounting_run_id"] != repeated["http_accounting_run_id"]
    assert (ds.directory / (result["raw_sha256"] + ".payload")).read_bytes() == RAW
    assert result["raw_sha256"] == hashlib.sha256(RAW).hexdigest()
    archived = ds.manifest()["captures"]
    assert len(archived) == 1 and "http_accounting" not in archived[0]
    runs, attempts = retained(ds)
    assert len(runs) == len(attempts) == 2
    assert runs[0]["capture_id"] == result["id"]
    assert (runs[0]["dataset"], runs[0]["asset"], runs[0]["quote"], runs[0]["timeframe"]) == (
        "repair-http", "BTC", "EUR", "1h")
    assert (runs[0]["source"], runs[0]["data_mode"], runs[0]["task"]) == ("kraken", "import", "history_rest")
    assert attempts[0]["http_accounting_run_id"] == result["http_accounting_run_id"]
    assert_private(runs, attempts, root)


@pytest.mark.parametrize("stage,requests_count", [("catalogue", 1), ("candles", 2)])
def test_failed_real_transport_retains_original_error_and_actual_attempts(tmp_path, stage, requests_count):
    ds = dataset(tmp_path)
    with transport() as (root, hits):
        client = LocalKraken(root, **{stage: "/" + stage + "-fail"})
        try:
            with pytest.raises(ccxt.BaseError) as raised:
                ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
        finally:
            client.close()
    assert len(hits) == requests_count
    metrics = raised.value.http_accounting
    assert metrics["totals"]["requests"] == metrics["totals"]["responses"] == requests_count
    assert metrics["totals"]["http_errors"] == 1
    assert metrics["totals"]["errors"] == 1
    assert raised.value.http_accounting_retention == "retained"
    runs, attempts = retained(ds)
    assert len(runs) == len(attempts) == 1
    assert runs[0]["status"] == attempts[0]["status"] == "error"
    assert runs[0]["errors"] == [type(raised.value).__name__]
    assert runs[0]["capture_id"] is None
    assert ds.manifest()["captures"] == []
    assert_private(runs, attempts, root)


def test_transport_retry_is_three_attempts_with_one_logical_fetch(tmp_path):
    ds = dataset(tmp_path)
    with transport() as (root, hits):
        client = LocalKraken(root, candles="/retry", retry=True)
        try:
            result = ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
        finally:
            client.close()
    assert hits == ["/catalogue", "/retry", "/retry"]
    assert result["logical_fetch_calls"] == 1
    assert result["http_accounting"]["totals"]["requests"] == 3
    assert result["http_accounting"]["totals"]["http_errors"] == 1
    assert retained(ds)[0][0]["logical_fetch_calls"] == 1


def test_broken_connection_counts_attempt_without_inventing_response(tmp_path):
    ds = dataset(tmp_path)
    with transport() as (root, hits):
        client = LocalKraken(root, catalogue="/disconnect")
        try:
            with pytest.raises(ccxt.NetworkError) as raised:
                ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
        finally:
            client.close()
    assert hits == ["/disconnect"]
    totals = raised.value.http_accounting["totals"]
    assert totals["requests"] == totals["errors"] == 1
    assert totals["responses"] == totals["decoded_body_bytes"] == 0
    assert retained(ds)[0][0]["http_accounting"] == raised.value.http_accounting


def test_unsupported_catalogue_keeps_observed_catalogue_traffic(tmp_path):
    ds = dataset(tmp_path)
    with transport() as (root, hits):
        client = LocalKraken(root, supported=False)
        try:
            with pytest.raises(UnsupportedMarket) as raised:
                ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
        finally:
            client.close()
    assert hits == ["/catalogue"]
    assert raised.value.http_accounting["totals"]["requests"] == 1
    assert raised.value.http_accounting["totals"]["errors"] == 0
    runs, attempts = retained(ds)
    assert runs[0]["capture_status"] == attempts[0]["status"] == "unsupported"
    assert_private(runs, attempts, root)


def test_injected_clients_and_custom_adapters_keep_unknown_http_totals(tmp_path):
    ds = dataset(tmp_path)
    client = SimpleNamespace(load_markets=lambda: {"BTC/EUR": {"spot": True}},
                             fetch_ohlcv=lambda *_a, **_k: ROWS)
    first = ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
    assert first["http_accounting"]["coverage"] == "uninstrumented"
    assert first["http_accounting"]["totals"] is None
    # A requests Session alone is insufficient: arbitrary adapters are unknown.
    with requests.Session() as session:
        session.close()
        session.adapters = {"http://": SimpleNamespace(close=lambda: None)}
        client.session = session
        second = ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
    assert second["http_accounting"]["totals"] is None
    assert second["http_accounting"]["coverage"] == "uninstrumented"

    def broken(*_args):
        raise RuntimeError("https://secret.invalid/path?token=secret-query secret-body")

    ds.plan_repair("BTC", "USD", 0, 3600)
    state = ds.repair("BTC", "USD", broken)
    assert state["error"] == "RuntimeError"
    runs, attempts = retained(ds)
    assert runs[-1]["http_accounting"]["totals"] is None
    assert_private(runs, attempts)


def test_history_runs_are_read_by_cli_mcp_and_follow_existing_retention(tmp_path, capsys):
    clock = [NOW]
    ds = dataset(tmp_path, clock=lambda: clock[0])
    with transport() as (root, _hits):
        client = LocalKraken(root)
        try:
            for _ in range(2):
                ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
                clock[0] += 1
        finally:
            client.close()
    # Retain a pending journal marker: inspection must neither replay nor write it.
    pending = ds.directory / "pending-inspection.json"
    pending.write_text("not a valid replay journal")
    # SQLite's WAL-aware read-only connections may create their own sidecars.
    def durable_files():
        return {p.name: p.read_bytes() for p in ds.directory.rglob("*")
                if p.is_file() and not p.name.endswith(("-wal", "-shm"))}

    before = durable_files()
    selection = {"source": "kraken", "task": "history_rest", "mode": "import", "end": NOW + 10}
    assert stored_http_accounting(ds.store, **selection)["summary"]["totals"]["requests"] == 3
    assert stored_http_accounting(ds.store, **{**selection, "mode": "live"})["included_runs"] == 0
    assert main(["http-accounting", "--data-dir", str(ds.directory), "--mode", "import",
                 "--source", "kraken", "--task", "history_rest"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["totals"]["requests"] == 3

    async def inspect():
        response = await create_server(Settings(data_dir=ds.directory), "import").call_tool(
            "get_http_accounting", {"source": "kraken", "task": "history_rest"})
        assert not response.isError
        assert response.structuredContent["summary"]["totals"]["requests"] == 3

    asyncio.run(inspect())
    assert durable_files() == before
    pending.unlink()
    policy = StoragePolicy(run_keep_days=1)
    maintenance = maintain_storage(ds.store, policy, apply=True, now=NOW + 3 * 86400)
    archived = next(f for f in maintenance["families"] if f["family"] == "runs")
    assert archived["deleted_rows"] == 1
    assert verify_archive(archived["archive"])["rows"][0]["payload"]
    remaining = stored_http_accounting(ds.store, **selection)
    assert remaining["included_runs"] == 1
    assert remaining["summary"]["totals"]["requests"] == 1
    assert "retained" in remaining["availability"]


def test_accounting_reservation_rejects_before_capture_and_survives_payload_budget_failure(tmp_path):
    ds = dataset(tmp_path)
    size = sum(p.stat().st_size for p in ds.directory.rglob("*") if p.is_file())
    ds.config = replace(ds.config, max_disk_bytes=size + 200_000)
    calls = []
    with pytest.raises(ValueError, match="disk budget"):
        ds.append_rest("BTC", "EUR", lambda *_: calls.append(1), since=0, limit=1)
    assert calls == []
    assert retained(ds) == ([], [])
    ds.config = replace(ds.config, max_disk_bytes=size + 300_000, max_capture_bytes=1)
    with pytest.raises(ValueError, match="payload budget") as raised:
        ds.append_rest("BTC", "EUR", lambda *_: RESTCapture(ROWS, RAW), since=0, limit=1)
    assert raised.value.http_accounting_retention == "retained"
    assert ds._reserved_bytes == 0
    assert len(retained(ds)[0]) == 1
    assert sum(p.stat().st_size for p in ds.directory.rglob("*") if p.is_file()) <= ds.config.max_disk_bytes


@pytest.mark.parametrize("fails", [False, True])
def test_unexpected_accounting_storage_failure_does_not_replace_capture_outcome(tmp_path, monkeypatch, fails):
    ds = dataset(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise OSError("secret-body")

    monkeypatch.setattr(ds, "_retain_rest_attempt", unavailable)
    original = RuntimeError("original fetch failure")

    def fetch(*_args):
        if fails:
            raise original
        return RESTCapture(ROWS, RAW)

    if fails:
        with pytest.raises(RuntimeError) as raised:
            ds.append_rest("BTC", "EUR", fetch, since=0, limit=1)
        assert raised.value is original
        outcome = vars(raised.value)
    else:
        outcome = ds.append_rest("BTC", "EUR", fetch, since=0, limit=1)
        assert len(ds.manifest()["captures"]) == 1
    assert outcome["http_accounting_retention"] == "error:OSError"
    assert outcome["http_accounting_run_id"] is None
    assert ds._reserved_bytes == 0


def test_run_and_history_attempt_commit_atomically_on_storage_failure(tmp_path):
    ds = dataset(tmp_path)
    with ds.store.connection() as con:
        con.execute("""CREATE TRIGGER fail_accounting BEFORE INSERT ON history_attempts
            BEGIN SELECT RAISE(ABORT, 'secret-body'); END""")
    result = ds.append_rest("BTC", "EUR", lambda *_: RESTCapture(ROWS, RAW), since=0, limit=1)
    assert result["http_accounting_retention"] == "error:IntegrityError"
    assert result["http_accounting_run_id"] is None
    assert retained(ds) == ([], [])
    assert len(ds.manifest()["captures"]) == 1


def test_recovery_preserves_transport_run_without_recounting_or_recovering_on_read(tmp_path, monkeypatch):
    ds = dataset(tmp_path)
    original = ds.store.put_candles

    def interrupted(candles):
        original(candles)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(ds.store, "put_candles", interrupted)
    with transport() as (root, _hits):
        client = LocalKraken(root)
        try:
            with pytest.raises(RuntimeError):
                ds.append_rest("BTC", "EUR", KrakenRESTCapture(client), since=0, limit=1)
        finally:
            client.close()
    assert len(list(ds.directory.glob("pending-*.json"))) == 1
    assert stored_http_accounting(ds.store, task="history_rest", mode="import")["summary"]["totals"]["requests"] == 2
    assert len(list(ds.directory.glob("pending-*.json"))) == 1
    recovered = HistoryDataset(ds.config, clock=lambda: NOW + 10)
    assert len(recovered.manifest()["captures"]) == 1
    assert len(retained(recovered)[0]) == 1
    assert not list(ds.directory.glob("pending-*.json"))


def test_nested_explicit_scope_restores_callers_measurements(tmp_path):
    ds = dataset(tmp_path)
    with accounting_scope("outer") as outer:
        attempt = outer.begin("outer", "GET", "https://example.invalid/outer")
        outer.finish(attempt, status=200, decoded_bytes=1)
        ds.append_rest("BTC", "EUR", lambda *_: RESTCapture(ROWS), since=0, limit=1)
        assert outer.snapshot("kraken")["totals"] is None
        assert outer.snapshot("outer")["totals"]["requests"] == 1
