import time

import pytest

from crypto_market_analysis.collectors import ExchangeClients
from crypto_market_analysis.config import Settings
from crypto_market_analysis.local_workflow import NATIVE_MARKETS, run_local_workflow
from crypto_market_analysis.models import Candle
from crypto_market_analysis.storage import Store


@pytest.fixture
def setup(tmp_path):
    now = int(time.time()) // 3600 * 3600
    settings = Settings(data_dir=tmp_path, venues=["kraken"], max_pages=2)
    store = Store(settings.database)
    return settings, store, now


def seed(store, now, *, source="kraken", mode="import", markets=NATIVE_MARKETS, missing=()):
    candles = []
    for market in markets:
        asset, quote = market.split("/")
        for hour in range(48):
            if hour in missing:
                continue
            opened = now - (48 - hour) * 3600
            candles.append(Candle(source, asset, quote, "1h", opened, opened + 3600,
                                  100, 101, 99, 100, 1, now, data_mode=mode))
    store.put_candles(candles)


def request(now, **overrides):
    return {"data_mode": "import", "sources": ["kraken"], "as_of": now,
            "required_history_days": 1, **overrides}


def test_offline_four_native_reports_are_read_only_and_network_free(setup, monkeypatch):
    settings, store, now = setup
    seed(store, now)
    monkeypatch.setattr("crypto_market_analysis.local_workflow.collect",
                        lambda *a, **kw: pytest.fail("offline workflow attempted collection"))
    before = store.counts()
    result = run_local_workflow(settings, request(now))
    assert result["status"] == "pass"
    assert {row["market"] for row in result["markets"]} == set(NATIVE_MARKETS)
    assert all(row["accumulation_brief"]["market"]["quote"] == row["market"].split("/")[1]
               for row in result["markets"])
    assert all(row["issuance"] == {"status": "not_requested"} for row in result["markets"])
    assert store.counts() == before
    with store.connection() as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None


def test_missing_source_keeps_other_native_reports_available(setup):
    settings, store, now = setup
    seed(store, now)
    result = run_local_workflow(settings, request(now, sources=["kraken", "coinbase"]))
    assert result["status"] == "partial"
    assert len(result["markets"]) == 8
    for report in result["markets"]:
        assert (report["accumulation_brief"] is not None) == (report["source"] == "kraken")


def test_sparse_history_suppresses_analytics_and_issuance(setup, monkeypatch):
    settings, store, now = setup
    seed(store, now, missing=(35,))
    monkeypatch.setattr("crypto_market_analysis.engine.Engine.capture_inputs",
                        lambda *a, **kw: pytest.fail("failed qualification reached analytics"))
    result = run_local_workflow(settings, request(now, issue_assessments=True, idempotency_key="sparse"))
    assert result["status"] == "unavailable"
    assert all(row["issuance"]["status"] == "suppressed" for row in result["markets"])
    assert all(row["analyses"] == {} for row in result["markets"])
    with store.connection() as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None


def test_no_fallback_between_data_modes(setup):
    settings, store, now = setup
    seed(store, now, mode="demo")
    result = run_local_workflow(settings, request(now, data_mode="live"))
    assert result["status"] == "unavailable"
    assert all(row["accumulation_brief"] is None for row in result["markets"])


def test_explicit_offline_demo_remains_synthetic_and_unqualified(setup):
    settings, store, now = setup
    seed(store, now, mode="demo")
    result = run_local_workflow(settings, request(now, data_mode="demo"))
    assert result["status"] == "partial"
    for row in result["markets"]:
        assert row["accumulation_brief"] is not None
        assert row["analytics_gate"] == {"allowed": True, "basis": "synthetic_demonstration_only",
                                         "real_source_qualified": False}
        assert row["data_mode"] == "demo"


def test_explicit_demo_issuance_stays_excluded_from_prospective_evidence(setup):
    from crypto_market_analysis.engine import Engine

    settings, store, now = setup
    seed(store, now, mode="demo", markets=["BTC/EUR"])
    result = run_local_workflow(settings, request(
        now, data_mode="demo", markets=["BTC/EUR"], issue_assessments=True,
        idempotency_key="synthetic-only"))
    [report] = result["markets"]
    assert report["analytics_gate"]["real_source_qualified"] is False
    assert report["issuance"]["status"] == "issued"
    original = report["issuance"]["assessment"]
    assert original["payload"]["input_reference"]["data_mode"] == "demo"
    summary = Engine(settings, "demo").journal_summary()
    assert summary["assessment_count"] == 1
    assert summary["counts"]["issued"] == summary["counts"]["excluded"] > 0


def test_demo_exception_does_not_bypass_history_gaps(setup):
    settings, store, now = setup
    seed(store, now, mode="demo", missing=(35,))
    result = run_local_workflow(settings, request(now, data_mode="demo"))
    assert all(row["accumulation_brief"] is None for row in result["markets"])


@pytest.mark.parametrize("budget", [{"max_storage_bytes": 1}, {"max_query_rows": 1}])
def test_resource_budget_failure_suppresses_analytics_and_issuance(setup, budget):
    settings, store, now = setup
    seed(store, now)
    result = run_local_workflow(settings, request(now, issue_assessments=True,
                                                 idempotency_key="bounded", **budget))
    assert result["status"] == "unavailable"
    assert all(not row["analytics_gate"]["allowed"] for row in result["markets"])
    assert all(row["issuance"]["status"] == "suppressed" for row in result["markets"])


def test_concurrent_candle_revision_invalidates_qualification(setup, monkeypatch):
    from crypto_market_analysis.engine import Engine

    settings, store, now = setup
    seed(store, now, markets=["BTC/EUR"])
    capture = Engine.capture_inputs

    def revised(self, *args, **kwargs):
        store.put_candles([Candle("kraken", "BTC", "EUR", "1h", now - 3600, now,
                                  100, 102, 99, 101, 1, now, data_mode="import")])
        return capture(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "capture_inputs", revised)
    result = run_local_workflow(settings, request(now, markets=["BTC/EUR"], issue_assessments=True,
                                                 idempotency_key="concurrent"))
    assert result["status"] == "unavailable"
    assert "changed after qualification" in result["markets"][0]["error"]
    assert not result["markets"][0]["analytics_gate"]["allowed"]
    assert result["markets"][0]["issuance"]["status"] == "suppressed"


@pytest.mark.parametrize("overrides", [
    {"refresh": True}, {"refresh": True, "data_mode": "live"},
    {"capture_books": True}, {"official_context": {"include": ["fx"]}},
    {"issue_assessments": True}, {"idempotency_key": "implicit"},
    {"refresh": "yes"}, {"required_history_days": 0}, {"max_logical_operations": 0},
    {"sources": ["kraken", "kraken"]}, {"markets": ["BTC/USDT"]},
    {"unexpected": True}, {"market_sources": {"BTC/EUR": ["unknown"]}},
    {"market_sources": None},
])
def test_invalid_request_fails_before_collection(setup, overrides, monkeypatch):
    settings, _, now = setup
    monkeypatch.setattr("crypto_market_analysis.local_workflow.collect",
                        lambda *a, **kw: pytest.fail("invalid request reached collection"))
    with pytest.raises(ValueError):
        run_local_workflow(settings, request(now, **overrides))


class PublicVenue:
    has = {"fetchOHLCV": True, "fetchOrderBook": True}

    def __init__(self, now, *, fail=False):
        self.now, self.fail, self.calls = now, fail, []

    def load_markets(self):
        self.calls.append("catalogue")
        if self.fail:
            raise OSError("HTTP 451 public access unavailable")
        return {market: {"symbol": market, "base": market.split("/")[0],
                         "quote": market.split("/")[1], "spot": True, "active": True}
                for market in NATIVE_MARKETS}

    def fetch_ohlcv(self, symbol, timeframe, *, since, limit):
        self.calls.append(symbol)
        return [[(self.now - (48 - hour) * 3600) * 1000, 100, 101, 99, 100, 1]
                for hour in range(48)]

    def fetch_order_book(self, symbol, *, limit):
        self.calls.append(f"book:{symbol}")
        return {"timestamp": self.now * 1000, "bids": [[99, 10]], "asks": [[101, 10]]}


def test_refresh_uses_actual_clock_exact_markets_and_shared_catalogue(setup):
    settings, store, now = setup
    venue = PublicVenue(now)
    with ExchangeClients() as clients:
        clients._clients["kraken"] = venue
        result = run_local_workflow(settings, {
            "refresh": True, "data_mode": "live", "sources": ["kraken"],
            "markets": ["BTC/EUR", "ETH/USD"], "required_history_days": 1,
            "history_days": 2, "max_pages": 1,
        }, clients=clients, clock=lambda: 0)
    assert result["as_of"] >= now
    assert venue.calls == ["catalogue", "BTC/EUR", "ETH/USD"]
    assert result["collection"]["budget"]["logical_operations"] == 3
    assert result["collection"]["budget"]["catalogue_operations"] == 1
    assert result["collection"]["budget"]["http_request_count"] is None
    assert {(c.asset, c.quote) for c in store.candles("BTC") + store.candles("ETH")} == {
        ("BTC", "EUR"), ("ETH", "USD")}


def test_budget_exhaustion_is_actual_failed_collection_not_success(setup):
    settings, store, now = setup
    venue = PublicVenue(now)
    with ExchangeClients() as clients:
        clients._clients["kraken"] = venue
        result = run_local_workflow(settings, {
            "refresh": True, "sources": ["kraken"], "markets": ["BTC/EUR"],
            "required_history_days": 1, "max_logical_operations": 1,
        }, clients=clients)
    assert venue.calls == ["catalogue"]
    assert result["status"] == "unavailable"
    collected = result["collection"]["market"]["markets"][0]
    assert collected["reports"][0]["errors"]
    assert "budget is exhausted" in collected["reports"][0]["errors"][0]
    assert store.candles("BTC") == []


def test_live_venue_outage_preserves_available_venue(setup):
    settings, _, now = setup
    with ExchangeClients() as clients:
        clients._clients.update(kraken=PublicVenue(now), coinbase=PublicVenue(now, fail=True))
        result = run_local_workflow(settings, {
            "refresh": True, "sources": ["kraken", "coinbase"], "markets": ["BTC/EUR"],
            "required_history_days": 1, "history_days": 2, "max_pages": 1,
        }, clients=clients)
    assert result["status"] == "partial"
    reports = {row["source"]: row for row in result["markets"]}
    assert reports["kraken"]["accumulation_brief"] is not None
    assert reports["coinbase"]["accumulation_brief"] is None
    assert "451" in str(result["collection"])


def test_explicit_issuance_retry_returns_original_after_gate_becomes_stricter(setup):
    settings, store, now = setup
    seed(store, now)
    payload = request(now, markets=["BTC/EUR"], issue_assessments=True, idempotency_key="one-shot")
    first = run_local_workflow(settings, payload)
    original = first["markets"][0]["issuance"]["assessment"]
    # A stricter data gate does not hide or duplicate an already issued original.
    retry = run_local_workflow(settings, {**payload, "required_history_days": 730})
    assert retry["markets"][0]["issuance"] == {"status": "original", "assessment": original}
    with store.connection() as con:
        assert con.execute("SELECT count(*) FROM assessment_journal").fetchone()[0] == 1


def test_reused_issuance_key_with_different_cutoff_is_rejected(setup):
    settings, store, now = setup
    seed(store, now)
    payload = request(now, markets=["BTC/EUR"], issue_assessments=True, idempotency_key="collision")
    run_local_workflow(settings, payload)
    with pytest.raises(ValueError, match="different request"):
        run_local_workflow(settings, {**payload, "as_of": now - 3600})


def test_retained_books_do_not_repeat_pipeline_liquidity_request(setup):
    settings, _, now = setup
    venue = PublicVenue(now)
    with ExchangeClients() as clients:
        clients._clients["kraken"] = venue
        result = run_local_workflow(settings, {
            "refresh": True, "sources": ["kraken"], "markets": ["BTC/EUR"],
            "required_history_days": 1, "history_days": 2, "max_pages": 1,
            "capture_tasks": ["market", "liquidity"], "capture_books": True,
        }, clients=clients)
    assert venue.calls.count("book:BTC/EUR") == 1
    assert result["collection"]["market"]["tasks"] == ["market"]
    assert result["collection"]["books"]["reports"][0]["captured_books"] == 1


def test_official_context_capture_persists_date_only_reference_without_fake_clock(setup):
    settings, store, now = setup
    seed(store, now, mode="live", markets=["BTC/EUR"])

    class OfficialHTTP:
        def get_bytes(self, url):
            return (b'<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
                    b'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
                    b'<gesmes:subject>Synthetic reference rates fixture</gesmes:subject>'
                    b'<Cube><Cube time="2026-10-28"><Cube currency="USD" rate="1.1234"/>'
                    b'</Cube></Cube></gesmes:Envelope>')

    result = run_local_workflow(settings, {
        "refresh": True, "sources": ["kraken"], "markets": ["BTC/EUR"],
        "required_history_days": 1, "capture_tasks": [],
        "official_context": {"include": ["fx"], "max_requests": 1},
    }, http_client=OfficialHTTP())
    fx = store.local_records("fx_reference")[0]
    assert fx["source_date"] == "2026-10-28"
    assert fx["event_time"] is None
    assert result["collection"]["official_context"]["status"] == "available"
    assert result["markets"][0]["analyses"]["quotes"]["fx_reference"]["status"] == "unknown_timestamp"
