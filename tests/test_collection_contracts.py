"""Native quote collection and bounded public-client lifecycle contracts."""

import time

import pytest

from crypto_market_analysis.collectors import ExchangeClients, collect_exchange, funding_interval_hours
from crypto_market_analysis.config import Settings, load_settings
from crypto_market_analysis.models import Candle, CollectionResult
from crypto_market_analysis.pipeline import collect
from crypto_market_analysis.providers import _date
from crypto_market_analysis.storage import Store
from crypto_market_analysis.venues import VENUES


class NativeMarkets:
    has = {"fetchOHLCV": True, "fetchOrderBook": True}

    def __init__(self):
        self.calls = []
        self.catalogue_calls = []
        self.closed = False

    def load_markets(self, reload=False):
        self.catalogue_calls.append(reload)
        return {
            quote: {"base": "BTC", "quote": quote, "spot": True, "symbol": f"BTC/{quote}"}
            for quote in ("EUR", "USD", "USDT", "USDC")
        }

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        self.calls.append((symbol, since))
        return [[since or 0, 100, 102, 99, 101, 1]]

    def fetch_order_book(self, symbol, limit=100):
        return {"bids": [[99, 1]], "asks": [[101, 2]]}

    def close(self):
        self.closed = True


def test_explicit_quotes_remain_independent_and_load_catalogue_once():
    client = NativeMarkets()
    result = collect_exchange(
        "kraken",
        ["BTC"],
        now=3600,
        include=("candles", "liquidity"),
        quotes=("EUR", "USD", "USDT", "USDC"),
        client=client,
    )
    assert {c.quote for c in result.candles} == {"EUR", "USD", "USDT", "USDC"}
    assert {o.quote for o in result.observations} == {"EUR", "USD", "USDT", "USDC"}
    assert client.catalogue_calls == [False]
    assert not client.closed  # Injected clients stay caller-owned.


def test_missing_requested_pair_never_uses_another_quote():
    client = NativeMarkets()
    client.load_markets = lambda: {
        "usdc": {"base": "BTC", "quote": "USDC", "spot": True, "symbol": "BTC/USDC"}
    }
    result = collect_exchange(
        "bitvavo", ["BTC"], now=3600, include=("candles",), quotes=("USD", "EUR"), client=client
    )
    assert not result.candles and not client.calls
    assert any("BTC/USD: no active" in w for w in result.warnings)
    assert any("BTC/EUR: no active" in w for w in result.warnings)


def test_bitvavo_defaults_to_eur_without_pruning_global_sources():
    result = collect_exchange("bitvavo", ["BTC"], now=3600, include=("candles",), client=NativeMarkets())
    assert [c.quote for c in result.candles] == ["EUR"]
    assert {
        "binance",
        "coinbase",
        "kraken",
        "okx",
        "bybit",
        "bitstamp",
        "bitfinex",
        "kucoin",
        "gate",
        "cryptocom",
        "gemini",
        "bitget",
        "mexc",
        "htx",
        "bitvavo",
    } <= set(VENUES)
    settings = Settings()
    assert settings.purchase_candidates == ["kraken", "bitvavo", "coinbase"]
    assert settings.venues == list(VENUES)
    assert settings.quote_currencies == ["EUR", "USD", "USDT", "USDC"]


def test_pair_errors_preserve_other_native_markets():
    class Partial(NativeMarkets):
        def fetch_ohlcv(self, symbol, *args, **kwargs):
            if symbol.endswith("EUR"):
                raise RuntimeError("temporary EUR outage")
            return super().fetch_ohlcv(symbol, *args, **kwargs)

    result = collect_exchange(
        "kraken", ["BTC"], now=3600, include=("candles",), quotes=("EUR", "USD"), client=Partial()
    )
    assert [c.quote for c in result.candles] == ["USD"]
    assert any("BTC/EUR" in e and "outage" in e for e in result.errors)


def test_resume_cursors_apply_to_each_pair():
    client = NativeMarkets()
    result = collect_exchange(
        "kraken",
        ["BTC"],
        since=0,
        now=10800,
        max_pages=1,
        include=("candles",),
        quotes=("EUR", "USD"),
        since_by_market={("BTC", "EUR"): 3600, ("BTC", "USD"): 7200},
        client=client,
    )
    assert client.calls == [("BTC/EUR", 3600000), ("BTC/USD", 7200000)]
    assert {(c.quote, c.open_time) for c in result.candles} == {("EUR", 3600), ("USD", 7200)}


def test_client_pool_reuses_rate_limiter_refreshes_catalogue_and_closes(monkeypatch):
    import ccxt

    made = []
    clock = [0.0]

    def factory(options):
        assert options["enableRateLimit"] and options["requests_trust_env"]
        client = NativeMarkets()
        made.append(client)
        return client

    monkeypatch.setattr(ccxt, "kraken", factory)
    monkeypatch.setattr("crypto_market_analysis.collectors.time.monotonic", lambda: clock[0])
    with ExchangeClients(catalogue_ttl=60) as pool:
        for _ in range(2):
            collect_exchange("kraken", ["BTC"], now=3600, include=("candles",), clients=pool)
        assert len(made) == 1
        assert made[0].catalogue_calls == [False]
        clock[0] = 61
        collect_exchange("kraken", ["BTC"], now=3600, include=("candles",), clients=pool)
        assert made[0].catalogue_calls == [False, True]
    assert made[0].closed


def test_pipeline_uses_quote_watermarks_and_liquidity_subset(monkeypatch, tmp_path):
    now = int(time.time())
    opened = now // 3600 * 3600 - 3600
    settings = Settings(
        data_dir=tmp_path,
        venues=["kraken", "binance"],
        quote_currencies=["EUR", "USD"],
        liquidity_venues=["kraken"],
    )
    store = Store(settings.database)
    store.save_result(
        CollectionResult(
            "kraken",
            candles=[Candle("kraken", "BTC", "USD", "1h", opened, opened + 3600, 100, 101, 99, 100, 1, now)],
        ),
        "market",
        now,
    )
    calls = {venue: [] for venue in settings.venues}

    def fake(venue, assets, **kwargs):
        calls[venue].append(kwargs)
        return CollectionResult(venue)

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", fake)
    report = collect(settings, store, tasks=("market", "liquidity"))
    assert len(report["reports"]) == 2
    for call in calls["kraken"]:
        assert call["quotes"] == ("EUR", "USD")
        assert call["since_by_market"][("BTC", "USD")] == opened - 3600
        assert call["since_by_market"][("BTC", "EUR")] < opened - 3600
    assert any("liquidity" in call["include"] for call in calls["kraken"])
    assert all("liquidity" not in call["include"] for call in calls["binance"])
    assert any("candles" in call["include"] for call in calls["binance"])


@pytest.mark.parametrize(
    "content",
    [
        'quote_currencies = ["EUR", "EUR"]',
        'quote_currencies = ["GUSD"]',
        'liquidity_venues = ["unknown"]',
        'purchase_jurisdiction = "unknown"',
    ],
)
def test_native_quote_and_scope_config_validation(tmp_path, content):
    path = tmp_path / "observer.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_settings(path)


def test_funding_intervals_and_oi_never_infer_unknown_units():
    class Derivatives(NativeMarkets):
        has = {"fetchFundingRate": True, "fetchFundingRateHistory": True, "fetchOpenInterest": True}

        def load_markets(self):
            return {"swap": {"base": "BTC", "quote": "USD", "swap": True, "symbol": "BTC/USD:USD"}}

        def fetch_funding_rate(self, symbol):
            return {"fundingRate": 0.01, "interval": "28800s"}

        def fetch_funding_rate_history(self, symbol, since=None, limit=100):
            return [{"fundingRate": 0.02, "timestamp": 0, "interval": "8h"}]

        def fetch_open_interest(self, symbol):
            return {"openInterestAmount": 3, "openInterestValue": 300}

    result = collect_exchange(
        "gate", ["BTC"], now=3600, include=("derivatives",), quotes=("USD",), client=Derivatives()
    )
    funding = [o for o in result.observations if o.metric.startswith("funding_rate")]
    assert {o.metric for o in funding} == {"funding_rate_projected", "funding_rate_actual"}
    assert all(o.metadata["funding_interval_seconds"] == 28800 for o in funding)
    assert all(
        o.unit == "unknown" and o.quote == "USD" for o in result.observations if "interest" in o.metric
    )
    assert funding_interval_hours({"interval": "8"}) is None
    assert not result.warnings


def test_provider_offsets_convert_to_utc_without_relabelling():
    assert _date("2026-10-28T08:30:00-04:00") == _date("2026-10-28T12:30:00Z")
    assert _date("2026-10-28") == _date("2026-10-28T00:00:00Z")


def test_zero_exchange_timestamp_does_not_refresh_old_evidence():
    from crypto_market_analysis.collectors import summarize_book

    observations = summarize_book(
        {"timestamp": 0, "bids": [[99, 1]], "asks": [[101, 1]]}, "kraken", "BTC", "EUR", 3600
    )
    assert all(o.event_time == 0 and o.metadata["timestamp_source"] == "exchange" for o in observations)
