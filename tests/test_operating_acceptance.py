"""Real finite pipeline execution on owned loopback; no provider qualification."""

import json
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import ccxt
import pytest

from crypto_market_analysis.collectors import ExchangeClients
from crypto_market_analysis.config import Settings
from crypto_market_analysis.cost_report import operating_cost_report
from crypto_market_analysis.models import Candle, CollectionResult
from crypto_market_analysis.operating_acceptance import (
    operating_acceptance_exit_code,
    operating_health,
    run_operating_acceptance,
)
from crypto_market_analysis.storage import Store


@contextmanager
def fixture_venue():
    end = int(time.time()) // 3600 * 3600
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            rows = [[(end-3600*(24-i))*1000, 100, 101, 99, 100, 1] for i in range(24)]
            body = json.dumps(rows if self.path == "/candles" else {}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    class Venue(ccxt.kraken):
        def __init__(self, root):
            super().__init__({"enableRateLimit": False})
            self.root = root
            self.has = {"fetchOHLCV": True}

        def load_markets(self, **kwargs):
            self.fetch(self.root + "/catalogue")
            return {"BTC/USD": {"symbol": "BTC/USD", "base": "BTC", "quote": "USD", "spot": True}}

        def fetch_ohlcv(self, *args, **kwargs):
            return self.fetch(self.root + "/candles")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExchangeClients() as clients:
            clients._clients["kraken"] = Venue(f"http://127.0.0.1:{server.server_port}")
            yield clients, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settings_for(tmp_path):
    return Settings(data_dir=tmp_path/"missing", assets=["BTC"], venues=["kraken"],
                    quote_currencies=["USD"], options=False, context=False, max_pages=1)


def test_missing_inspection_never_initializes_store_or_reports(tmp_path):
    settings = settings_for(tmp_path)
    result = run_operating_acceptance(settings, {})
    assert result["status"] == "not_recorded"
    assert result["cost"]["rows"] == result["health"]["rows"] == []
    assert not settings.data_dir.exists()


def test_loopback_cycles_resume_actual_receipts_and_keep_transport_provenance(tmp_path):
    settings = settings_for(tmp_path)
    request = {"refresh": True, "session_id": "prospective", "max_cycles": 1,
               "max_duration_seconds": 2, "interval_seconds": 1}
    with fixture_venue() as (clients, hits):
        first = run_operating_acceptance(settings, request, clients=clients)
        receipt = first["last_cycle"]["as_of"]
        second = run_operating_acceptance(settings, request, clients=clients)
    assert hits == ["/catalogue", "/candles", "/candles"]
    assert second["manifest"]["completed_cycles"] == 2
    assert 0 <= second["manifest"]["elapsed_wall_seconds"] < 30
    assert second["last_cycle"]["as_of"] >= receipt
    assert second["last_cycle"]["provider_acceptance"] == "unverified_injected_transport"
    assert second["last_cycle"]["quality"]["markets"][0]["status"] == "pass"
    assert second["last_cycle"]["resources"]["row_growth"]["candles"] == 0
    assert second["last_cycle"]["resources"]["row_growth"]["runs"] == 1
    directory = settings.data_dir / "operating_acceptance" / "prospective"
    assert len(list(directory.glob("cycle-*.json"))) == 2
    cost = operating_cost_report(settings.database, as_of=second["last_cycle"]["as_of"])
    assert cost["rows"][0]["requests"] == 3
    assert cost["rows"][0]["decoded_body_bytes"] > 0
    assert cost["actual_bill"] is None
    assert operating_health(settings.database, as_of=receipt-1)["rows"] == []
    assert run_operating_acceptance(settings, {"session_id": "prospective"})["manifest"]["completed_cycles"] == 2


def test_one_shared_attempt_budget_includes_catalogue_and_refuses_candle_request(tmp_path):
    settings = settings_for(tmp_path)
    with fixture_venue() as (clients, hits):
        report = run_operating_acceptance(settings, {"refresh": True, "cycle_http_requests": 1}, clients=clients)
    assert hits == ["/catalogue"]
    cycle = report["last_cycle"]
    assert cycle["collection"]["http_limits"]["used_requests"] == 1
    assert cycle["status"] == "budget_exhausted"
    assert cycle["quality"]["markets"][0]["status"] == "unknown"
    assert operating_cost_report(settings.database)["rows"][0]["requests"] == 1


def test_session_refuses_selection_changes_without_new_capture(tmp_path):
    settings = settings_for(tmp_path)
    with fixture_venue() as (clients, hits):
        run_operating_acceptance(settings, {"refresh": True}, clients=clients)
        with pytest.raises(ValueError, match="selection changed"):
            run_operating_acceptance(replace(settings, assets=["ETH"]), {"refresh": True}, clients=clients)
    assert hits == ["/catalogue", "/candles"]


def test_known_at_native_quotes_gaps_and_row_budget_remain_independent(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    end = 100*3600
    def candle(hour, quote="USD", receipt=end):
        return Candle("fixture", "BTC", quote, "1h", end-hour*3600, end-(hour-1)*3600,
                      1, 1, 1, 1, 1, receipt)
    store.put_candles([candle(3), candle(1), candle(2, receipt=end+1), candle(3, "EUR")])
    rows = operating_health(store.path, as_of=end, required_history_hours=3)["rows"]
    by_quote = {row["quote"]: row for row in rows}
    assert by_quote["USD"]["coverage"]["missing_hours"] == 1
    assert by_quote["USD"]["coverage"]["largest_gap_hours"] == 1
    assert by_quote["EUR"]["coverage"]["missing_hours"] == 2
    assert by_quote["EUR"]["freshness"]["latest_closed_at"] == end-7200
    assert operating_health(store.path, as_of=end+1, required_history_hours=3)["rows"][1]["status"] == "pass"
    assert operating_health(store.path, as_of=end, max_query_rows=1)["rows"][1]["reason"] == "query_row_budget_exceeded"


def test_cost_sql_aggregates_all_retained_runs_and_freezes_group_pages(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    meter = {"coverage": "observed", "totals": {"requests": 2, "responses": 1, "errors": 1,
        "http_errors": 0, "decoded_body_bytes": 1024, "unknown_body_responses": 0, "elapsed_ms": 6}}
    payload = store.record_run(CollectionResult("a", http_accounting=meter), "market", 100, data_mode="live")
    with store.connection() as con:
        con.executemany("INSERT INTO runs(source,task,observed_at,payload) VALUES(?,?,?,?)",
            (("a", "market", 100, json.dumps(payload)) for _ in range(10001)))
    store.record_run(CollectionResult("legacy"), "market", 100)
    store.record_run(CollectionResult("z"), "context", 100, data_mode="live")
    first = operating_cost_report(store.path, "all", as_of=100, limit=1,
        prices={"currency": "EUR", "per_million_requests": 1})
    assert first["rows"][0]["retained_runs"] == 10002
    assert first["rows"][0]["requests"] == 20004
    assert first["rows"][0]["mean_attempt_latency_ms"] == 3
    assert first["rows"][0]["estimated_network_cost"]["request_cost"] == 0.020004
    store.record_run(CollectionResult("b", http_accounting=meter), "market", 99, data_mode="live")
    second = operating_cost_report(store.path, "all", as_of=100, limit=1,
        prices={"currency": "EUR", "per_million_requests": 1}, cursor=first["pagination"]["next_cursor"])
    assert second["rows"][0]["source"] == "legacy"
    assert second["rows"][0]["requests"] is None
    assert second["rows"][0]["measurement_coverage"] == "unknown"
    assert operating_cost_report(store.path, "unknown", as_of=100)["rows"][0]["source"] == "legacy"
    with pytest.raises(ValueError, match="changed selection"):
        operating_cost_report(store.path, "live", cursor=first["pagination"]["next_cursor"])


@pytest.mark.parametrize("value", [{"as_of": 0}, {"max_cycles": True}, {"session_id": "../escape"},
    {"cycle_deadline_seconds": float("nan")}, {"tasks": ["market", "market"]}])
def test_invalid_requests_do_not_initialize(tmp_path, value):
    settings = settings_for(tmp_path)
    with pytest.raises(ValueError):
        run_operating_acceptance(settings, value)
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("changes", [
    {"liquidity_assets": ["BTC"]}, {"liquidity_venues": []}, {"history_days": 90}, {"max_pages": 2},
    {"options": True}, {"context": True}, {"workers": 4}, {"collection_batch_size": 11},
    {"collection_work_limit": 100}, {"http_max_requests": 999}, {"http_deadline_seconds": 90},
    {"market_interval": 7200}, {"liquidity_interval": 600}, {"storage_run_keep_days": 10},
])
def test_session_fingerprint_freezes_complete_workload_settings(tmp_path, monkeypatch, changes):
    from crypto_market_analysis import operating_acceptance as operation

    settings = settings_for(tmp_path)
    calls = []
    def collect(scoped, *_args, **_kwargs):
        calls.append(scoped)
        return {"status": "ok", "reports": []}
    monkeypatch.setattr(operation, "collect", collect)
    first = run_operating_acceptance(settings, {"refresh": True}, clients=object())
    manifest = first["manifest"]
    assert manifest["selection"]["settings"]["max_pages"] == settings.max_pages
    assert manifest["selection"]["software"]["source_sha256"]["pipeline"]
    with pytest.raises(ValueError, match="selection changed"):
        run_operating_acceptance(replace(settings, **changes), {"refresh": True}, clients=object())
    assert len(calls) == 1
    persisted = json.loads((settings.data_dir/"operating_acceptance/default/manifest.json").read_text())
    assert persisted["completed_cycles"] == 1
    assert persisted["request_sha256"] == manifest["request_sha256"]


def test_row_counts_are_snapshot_bounded_and_unknown_after_sql_interruption(tmp_path, monkeypatch):
    from crypto_market_analysis import operating_acceptance as operation

    missing = tmp_path / "missing/observer.sqlite"
    assert operation._row_counts(missing) == {"candles": None, "observations": None, "runs": None}
    assert not missing.parent.exists()
    store = Store(tmp_path/"observer.sqlite")
    real_limit = operation.limit_database_read
    def interrupt(con, *, query_seconds):
        assert con.in_transaction
        real_limit(con, query_seconds=query_seconds)
        con.set_progress_handler(lambda: 1, 1)
    monkeypatch.setattr(operation, "limit_database_read", interrupt)
    assert operation._row_counts(store.path) == {"candles": None, "observations": None, "runs": None}


def test_setup_read_time_reduces_http_allowance_and_exhausted_reads_stay_unknown(tmp_path, monkeypatch):
    from crypto_market_analysis import operating_acceptance as operation

    clock = [1000.0]
    monkeypatch.setattr(operation, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    budgets, deadlines = [], []
    def counts(_database, *, query_seconds):
        budgets.append(query_seconds)
        if query_seconds <= 0:
            return {"candles": None, "observations": None, "runs": None}
        clock[0] += 0.9
        return {"candles": 0, "observations": 0, "runs": 0}
    def collect(scoped, *_args, **_kwargs):
        deadlines.append(scoped.http_deadline_seconds)
        assert 0 < scoped.http_deadline_seconds <= 0.101
        return {"status": "ok", "reports": []}
    def health(_settings, _request, _as_of, *, deadline):
        assert deadline == 1001
        clock[0] = deadline
        return {"status": "unknown", "reason": "inspection_budget_exhausted"}
    monkeypatch.setattr(operation, "_row_counts", counts)
    monkeypatch.setattr(operation, "collect", collect)
    monkeypatch.setattr(operation, "_cycle_health", health)
    result = run_operating_acceptance(settings_for(tmp_path), {"refresh": True, "max_duration_seconds": 1}, clients=object())
    assert len(deadlines) == 1
    assert budgets[-1] <= 0
    assert all(value is None for value in result["last_cycle"]["resources"]["row_growth"].values())
    assert result["invocation_elapsed_seconds"] == 1
    assert result["last_cycle"]["elapsed_seconds"] == 1


def test_restart_keeps_durable_cooldown_within_finite_invocation(tmp_path, monkeypatch):
    from crypto_market_analysis import operating_acceptance as operation

    calls = []
    def collect(*_args, **_kwargs):
        calls.append(True)
        return {"status": "ok", "reports": []}
    monkeypatch.setattr(operation, "collect", collect)
    settings = settings_for(tmp_path)
    first = run_operating_acceptance(settings, {"refresh": True}, clients=object())
    next_due = first["manifest"]["next_due"]
    second = run_operating_acceptance(settings, {"refresh": True, "max_duration_seconds": 1}, clients=object())
    assert second["status"] == "no_cycle_due"
    assert second["completed_this_invocation"] == 0
    assert second["manifest"]["next_due"] == next_due
    assert second["manifest"]["completed_cycles"] == 1
    assert 0.9 <= second["invocation_elapsed_seconds"] < 2
    assert len(calls) == 1


@pytest.mark.parametrize(("status", "code"), [("budget_exhausted", 1), ("error", 1), ("partial", 1),
    ("blocked", 1), ("ok", 0)])
def test_completed_cycle_count_does_not_mask_failed_capture_exit_status(status, code):
    assert operating_acceptance_exit_code({"status": "completed", "completed_this_invocation": 1,
                                          "last_cycle": {"status": status}}) == code
