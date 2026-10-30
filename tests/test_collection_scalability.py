"""Bounded multiasset collection and restart fairness without public requests."""

import pytest

from crypto_market_analysis.asset_universe import validate_asset, validate_assets
from crypto_market_analysis.collection_plan import collection_plan
from crypto_market_analysis.collectors import ExchangeClients
from crypto_market_analysis.config import Settings, load_settings
from crypto_market_analysis.http_limits import current_http_budget
from crypto_market_analysis.models import CollectionResult
from crypto_market_analysis.pipeline import collect
from crypto_market_analysis.scheduling import Schedule
from crypto_market_analysis.storage import Store


def settings_for(tmp_path, count, **kwargs):
    return Settings(data_dir=tmp_path, assets=[f"ASSET{i:04}" for i in range(count)],
                    venues=["kraken"], quote_currencies=["USD"], options=False, context=False,
                    **kwargs)


@pytest.mark.parametrize("value", ["btc", "BTC/USD", "", " USD", None, "EUR", "A" * 33])
def test_asset_identity_requires_explicit_canonical_code(value):
    with pytest.raises(ValueError):
        validate_asset(value)


def test_explicit_universe_limit_and_native_quotes(tmp_path):
    assert validate_assets(["BTC", "ETH", "1000SATS", "TOKEN.X"]) == ["BTC", "ETH", "1000SATS", "TOKEN.X"]
    for assets in (["BTC", "BTC"], [], [f"A{i}" for i in range(1001)]):
        with pytest.raises(ValueError):
            validate_assets(assets)
    path = tmp_path / "observer.toml"
    path.write_text('assets = ["BTC", "ETH", "SOL"]\nliquidity_assets = ["SOL"]\n')
    settings = load_settings(path)
    assert settings.assets == ["BTC", "ETH", "SOL"]
    assert settings.quote_currencies == ["EUR", "USD", "USDT", "USDC"]
    assert load_settings().assets == ["BTC", "ETH"]


def test_empty_100_asset_collection_caches_catalogue_and_preserves_unknown(monkeypatch, tmp_path):
    class Catalogue:
        has = {}
        calls = 0

        def load_markets(self):
            self.calls += 1
            return {"BTC/USD": {"base": "BTC", "quote": "USD", "spot": True, "symbol": "BTC/USD"}}

    settings = settings_for(tmp_path, 100)
    store = Store(settings.database)
    # No latest-per-asset SQL queries and no unbounded executor/future creation.
    monkeypatch.setattr(store, "latest_candle_time", lambda *a, **kw: pytest.fail("scalar cursor query"))
    monkeypatch.setattr("crypto_market_analysis.pipeline.ThreadPoolExecutor", lambda *a, **kw: pytest.fail("executor"))
    client = Catalogue()
    with ExchangeClients() as clients:
        clients._clients["kraken"] = client
        report = collect(settings, store, tasks=("market",), clients=clients)
    assert client.calls == 1
    assert report["collection_progress"]["work_attempted"] == 100
    assert not report["collection_progress"]["pending"]
    assert report["reports"][0]["status"] == "empty"
    assert len(report["reports"][0]["warnings"]) == 200
    assert store.counts()["candles"] == 0
    assert "unknown" in report["reports"][0]["warnings"][1]


def test_1000_assets_resume_under_small_shared_budget_including_failures(monkeypatch, tmp_path):
    settings = settings_for(tmp_path, 1000, collection_work_limit=13, http_max_requests=7)
    attempted = []

    def exchange(source, assets, **kwargs):
        current_http_budget().admit()
        attempted.append(assets[0])
        return CollectionResult(source, errors=["unsupported symbol"] if len(attempted) % 17 == 0 else [])

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", exchange)
    reports = []
    for _ in range(143):
        # A fresh Store and fresh progress reader model a restarted process.
        report = collect(settings, Store(settings.database), tasks=("market",))
        reports.append(report)
        assert report["collection_progress"]["work_attempted"] <= 7
    assert attempted == settings.assets
    assert all(report["collection_progress"]["pending"] for report in reports[:-1])
    assert not reports[-1]["collection_progress"]["pending"]
    assert any(row["status"] == "error" for report in reports for row in report["reports"])


def test_one_request_budget_rotates_sources_tasks_and_native_pairs(monkeypatch, tmp_path):
    settings = settings_for(tmp_path, 100, http_max_requests=1)
    settings.venues = ["kraken", "coinbase"]
    settings.quote_currencies = ["EUR", "USD"]
    attempted = []

    def exchange(source, assets, **kwargs):
        current_http_budget().admit()
        attempted.append((source, kwargs["include"], assets[0], kwargs["quotes"][0]))
        return CollectionResult(source)

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", exchange)
    for _ in range(8):
        collect(settings, Store(settings.database), tasks=("market", "liquidity"))
    assert len(set(attempted)) == 8
    assert {row[0] for row in attempted} == {"kraken", "coinbase"}
    assert {row[1] for row in attempted} == {("candles", "derivatives"), ("liquidity",)}
    assert [row[3] for row in attempted[:4]] == ["EUR"] * 4
    assert [row[3] for row in attempted[4:]] == ["USD"] * 4


def test_plan_exposes_dimensions_estimate_and_book_subset(tmp_path):
    settings = settings_for(tmp_path, 100)
    settings.venues = ["kraken", "coinbase"]
    settings.quote_currencies = ["EUR", "USD", "USDT", "USDC"]
    settings.liquidity_assets = settings.assets[:3]
    settings.liquidity_venues = ["kraken"]
    plan = collection_plan(settings)
    assert plan["market_work_units"] == 800
    assert plan["liquidity_work_units"] == 12
    assert plan["logical_calls_upper_estimate_per_rotation"]["books"] == 12
    assert plan["actual_http_requests"] is None
    assert not plan["specialized_options_assets"]
    assert len(plan["specialized_options_unavailable_or_unknown"]) == 100


def test_pending_rotation_is_due_soon_without_claiming_success(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    schedule = Schedule(store, {"market": 3600})
    schedule.record({"source": "kraken", "errors": [], "warnings": ["pending_collection_work"]}, "market", 100)
    state = Schedule(Store(store.path), {"market": 3600}).states()[("kraken", "market")]
    assert state["next_due"] == 105 and state["last_success"] == 0
    schedule.record({"source": "kraken", "errors": [], "warnings": []}, "market", 200)
    assert schedule.states()[("kraken", "market")]["next_due"] == 3800
