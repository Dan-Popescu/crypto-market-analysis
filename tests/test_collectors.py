from crypto_market_analysis.collectors import collect_exchange, parse_candles, select_market, summarize_book
from crypto_market_analysis.providers import collect_context, collect_options, parse_options
from crypto_market_analysis.venues import VENUES


def test_catalogue_unverified():
    assert len(VENUES) >= 14
    assert all(v.mica_status == "unverified" and v.official_source is None for v in VENUES.values())


def test_closed_candles_and_native_quote():
    rows = [[0, 10, 12, 9, 11, 1], [3600000, 11, 13, 10, 12, 2], [7200000, 12, 14, 11, 13, 3]]
    candles = parse_candles(rows, "x", "BTC", "USDT", 7200)
    assert [c.open_time for c in candles] == [0, 3600]
    assert all(c.quote == "USDT" for c in candles)


def test_market_preference_and_spot_swap_separation():
    markets = {
        "swap": {"base": "BTC", "quote": "USD", "swap": True, "symbol": "BTC/USD:BTC"},
        "usdt": {"base": "BTC", "quote": "USDT", "spot": True, "symbol": "BTC/USDT"},
        "eur": {"base": "BTC", "quote": "EUR", "spot": True, "symbol": "BTC/EUR"},
    }
    assert select_market(markets, "BTC")["symbol"] == "BTC/USDT"
    assert select_market(markets, "BTC", "swap")["symbol"] == "BTC/USD:BTC"


def test_sparse_book_reports_coverage():
    obs = summarize_book({"bids": [[99.9, 2]], "asks": [[100.1, 3]]}, "x", "BTC", "USDT", 100)
    depth = next(o for o in obs if o.metric == "depth_bid_2pct")
    assert depth.value == 199.8
    assert not depth.metadata["coverage_complete"]
    assert depth.unit == "USDT"


class Exchange:
    has = {"fetchOHLCV": True, "fetchOrderBook": True, "fetchFundingRate": False, "fetchOpenInterest": False}

    def load_markets(self):
        return {"spot": {"base": "BTC", "quote": "USDT", "spot": True, "symbol": "BTC/USDT"}}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        if since == 0:
            return [[0, 10, 12, 9, 11, 1]]
        return [[3600000, 11, 13, 10, 12, 2]]

    def fetch_order_book(self, symbol, limit=100):
        raise RuntimeError("restricted")


def test_exchange_partial_failure_and_pagination_bound():
    result = collect_exchange("binance", ["BTC"], since=0, now=20000, max_pages=2, client=Exchange())
    assert len(result.candles) == 2
    assert any("restricted" in e for e in result.errors)
    assert any("pagination limit" in w for w in result.warnings)
    assert any("no active supported swap" in w for w in result.warnings)


def test_option_expiry_and_liquidity():
    now = 1_000_000
    expiry = now + 8 * 86400
    instruments = [
        {
            "instrument_name": "BTC-C",
            "expiration_timestamp": expiry * 1000,
            "strike": 100,
            "option_type": "call",
        }
    ]
    summaries = [
        {
            "instrument_name": "BTC-C",
            "open_interest": 3,
            "bid_price": 1,
            "ask_price": 1.1,
            "mark_iv": 50,
            "underlying_price": 100,
        }
    ]
    obs = parse_options(instruments, summaries, "BTC", now)
    assert next(o for o in obs if o.metric == "option_atm_iv_7d").metadata["expiry"] == expiry
    assert next(o for o in obs if o.metric == "option_open_interest_expiry").value == 3
    summaries[0]["ask_price"] = 100
    assert not any(
        o.metric.startswith("option_atm_iv") for o in parse_options(instruments, summaries, "BTC", now)
    )


class BrokenHTTP:
    def get_json(self, url, params=None):
        raise RuntimeError("unavailable")

    def get_text(self, url, params=None):
        raise RuntimeError("unavailable")


def test_provider_failures_visible_and_isolated():
    assert len(collect_options(["BTC", "ETH"], now=100, client=BrokenHTTP()).errors) == 2
    results = collect_context(now=100000, days=1, client=BrokenHTTP())
    assert [len(r.errors) for r in results] == [4, 1, 5]
    assert not any(r.observations for r in results)


def test_binance_raw_quote_volume_and_nonfinite_rejected():
    row = [0, "10", "12", "9", "11", "2", 3599999, "22", 10, "1", "11", "0"]
    candle = parse_candles([row], "binance", "BTC", "USDT", 3600, binance_raw=True)[0]
    assert candle.quote_volume == 22 and candle.taker_buy_quote_volume == 11
    row[4] = "nan"
    assert not parse_candles([row], "binance", "BTC", "USDT", 3600, binance_raw=True)


def test_option_maturity_bound_and_skew():
    now = 1_000_000
    expiry = now + 60 * 86400
    instruments = [
        {"instrument_name": "P", "expiration_timestamp": expiry * 1000, "strike": 90, "option_type": "put"},
        {"instrument_name": "C", "expiration_timestamp": expiry * 1000, "strike": 110, "option_type": "call"},
    ]
    summaries = [
        {
            "instrument_name": name,
            "open_interest": 3,
            "bid_price": 1,
            "ask_price": 1.1,
            "mark_iv": iv,
            "underlying_price": 100,
        }
        for name, iv in [("P", 60), ("C", 50)]
    ]
    obs = parse_options(instruments, summaries, "BTC", now)
    assert not any(o.metric == "option_atm_iv_7d" for o in obs)
    assert next(o for o in obs if o.metric == "option_open_interest_expiry").unit == "BTC"
    skew = next(o for o in obs if o.metric == "option_downside_minus_upside_iv")
    assert skew.value == 10 and skew.metadata["expiry"] == expiry


def test_http_403_no_retry_and_503_retries(monkeypatch):
    import httpx

    from crypto_market_analysis.providers import PublicHTTP

    monkeypatch.setattr("crypto_market_analysis.providers.time.sleep", lambda _: None)

    class Fake:
        def __init__(self, statuses):
            self.statuses, self.calls = statuses, 0

        def get(self, url, params=None):
            status = self.statuses[self.calls]
            self.calls += 1
            return httpx.Response(status, request=httpx.Request("GET", url))

    client = PublicHTTP.__new__(PublicHTTP)
    client.client = Fake([403, 200])
    try:
        client.get_text("https://example.com")
    except httpx.HTTPStatusError:
        pass
    assert client.client.calls == 1
    client.client = Fake([503, 200])
    client.get_text("https://example.com")
    assert client.client.calls == 2


def test_funding_interval_is_not_assumed():
    from crypto_market_analysis.collectors import funding_interval_hours

    assert funding_interval_hours({"interval": "8h"}) == 8
    assert funding_interval_hours({}) is None
    assert funding_interval_hours({"interval": "8"}) is None


def test_ccxt_client_respects_environment_proxy(monkeypatch):
    import ccxt

    constructor = ccxt.kraken
    clients = []

    def make_client(options):
        exchange = constructor(options)
        clients.append(exchange)
        monkeypatch.setattr(exchange, "load_markets", lambda: {})
        return exchange

    monkeypatch.setattr(ccxt, "kraken", make_client)
    collect_exchange("kraken", [], include=())
    assert clients[0].session.trust_env is True
    clients[0].close()


def test_live_clock_tracks_time_after_fetch(monkeypatch):
    import crypto_market_analysis.collectors as collectors

    class LiveBook(Exchange):
        def fetch_order_book(self, symbol, limit=100):
            return {"timestamp": 101000, "bids": [[99, 1]], "asks": [[101, 1]]}

    clock = iter([100, 101])
    monkeypatch.setattr(collectors.time, "time", lambda: next(clock))
    result = collect_exchange("kraken", ["BTC"], include=("liquidity",), client=LiveBook())
    assert result.observations[0].event_time == 101
    assert result.observations[0].observed_at == 101
    fixed = collect_exchange("kraken", ["BTC"], now=100, include=("liquidity",), client=LiveBook())
    assert not fixed.observations


def test_venue_specific_book_limit():
    class Book(Exchange):
        def fetch_order_book(self, symbol, limit=100):
            assert limit == 150
            return {"bids": [[99, 1]], "asks": [[101, 1]]}

    result = collect_exchange("htx", ["BTC"], now=100, include=("liquidity",), client=Book())
    assert result.observations and not result.errors
    assert VENUES["kraken"].book_limit == 500
    assert VENUES["okx"].book_limit == 400


def test_empty_market_catalogue_is_source_error():
    class Empty(Exchange):
        def load_markets(self):
            return {}

    result = collect_exchange("kucoin", ["BTC"], client=Empty())
    assert "empty market catalogue" in result.errors[0]
    assert not result.candles


def test_venue_quote_preference_and_default():
    markets = {
        "usd": {"base": "BTC", "quote": "USD", "spot": True, "symbol": "BTC/USD"},
        "usdt": {"base": "BTC", "quote": "USDT", "spot": True, "symbol": "BTC/USDT"},
    }
    assert select_market(markets, "BTC")["quote"] == "USD"
    assert select_market(markets, "BTC", quotes=VENUES["gate"].preferred_quotes)["quote"] == "USDT"
    assert select_market(markets, "BTC", quotes=VENUES["kraken"].preferred_quotes)["quote"] == "USD"

    class TwoBooks(Exchange):
        def load_markets(self):
            return markets

        def fetch_order_book(self, symbol, limit=100):
            assert symbol == "BTC/USDT"
            return {"bids": [[99, 1]], "asks": [[101, 1]]}

    result = collect_exchange("gate", ["BTC"], now=100, include=("liquidity",), client=TwoBooks())
    assert result.observations and all(o.quote == "USDT" for o in result.observations)
