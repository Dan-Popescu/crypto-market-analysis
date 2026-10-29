"""Incremental cursors and durable per-source scheduling, without public calls."""

from datetime import UTC, datetime
from threading import Event

import httpx
import pytest

from crypto_market_analysis.collectors import ExchangeClients
from crypto_market_analysis.config import Settings
from crypto_market_analysis.local_workflow import run_local_workflow
from crypto_market_analysis.models import Candle, CollectionResult
from crypto_market_analysis.pipeline import collect, run_scheduler
from crypto_market_analysis.providers import PublicHTTP, RetryDeferred, collect_context
from crypto_market_analysis.scheduling import Schedule
from crypto_market_analysis.storage import Store


class Once(Event):
    def wait(self, timeout=None):
        self.set()
        return True


def test_local_refresh_incremental_with_explicit_gap_repair(monkeypatch, tmp_path):
    now = 1_800_000_000
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken"], assets=["BTC"], quote_currencies=["EUR"])
    cursors = []
    opened = now - 7200

    def exchange(source, assets, **kwargs):
        cursors.append(kwargs["since_by_market"][("BTC", "EUR")])
        return CollectionResult(source, candles=[Candle(source, "BTC", "EUR", "1h", opened,
                                opened + 3600, 1, 1, 1, 1, 1, now)])

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", exchange)
    payload = {"refresh": True, "sources": ["kraken"], "markets": ["BTC/EUR"],
               "required_history_days": 1, "history_days": 730, "analyses": ["quotes"]}
    with ExchangeClients() as clients:
        run_local_workflow(settings, payload, clients=clients)
        run_local_workflow(settings, payload, clients=clients)
        # A large historical gap remains explicit repair work, not every refresh.
        run_local_workflow(settings, {**payload, "backfill_days": 90}, clients=clients)
    assert cursors == [now - 730 * 86400, opened - 3600, now - 90 * 86400]
    assert Store(settings.database).counts()["candles"] == 1


def test_stale_series_resumes_from_latest_not_bootstrap_cutoff(monkeypatch, tmp_path):
    now = 1_800_000_000
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken"], assets=["BTC"], quote_currencies=["EUR"])
    store = Store(settings.database)
    old = now - 100 * 86400
    store.put_candles([Candle("kraken", "BTC", "EUR", "1h", old, old + 3600, 1, 1, 1, 1, 1, old + 3600)])
    requested = []
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", lambda source, *a, **kw:
                        requested.append(kw["since_by_market"][("BTC", "EUR")]) or CollectionResult(source))
    collect(settings, store, tasks=("market",))
    assert requested == [old - 3600]


def test_restart_keeps_slow_tasks_beyond_200_run_groups(monkeypatch, tmp_path):
    now = 1_800_000_000
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken"])
    store = Store(settings.database)
    for source, task in [("kraken", "market+liquidity"), ("deribit", "options"),
                         ("fred", "context"), ("coinmetrics", "context"), ("defillama", "context"),
                         ("storage", "retention")]:
        store.record_run(CollectionResult(source), task, now - 10)
    for i in range(250):
        store.record_run(CollectionResult(f"extra-{i}"), "unrelated", now)
    assert not any(row["task"] == "context" for row in store.runs(limit=200))
    calls = []
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect", lambda *a, **kw: calls.append(kw))
    run_scheduler(settings, store, Once())
    run_scheduler(settings, Store(settings.database), Once())
    assert calls == []
    assert Schedule(store, {"context": 86400}).states()[("fred", "context")]["last_success"] == now - 10


def test_source_failure_backoff_and_restart_do_not_repeat_healthy_source(monkeypatch, tmp_path):
    now = 1_800_000_000
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken", "coinbase"], options=False, context=False,
                        liquidity_venues=[])
    store = Store(settings.database)
    store.record_run(CollectionResult("storage"), "retention", now)
    calls = []

    def exchange(source, *args, **kwargs):
        calls.append(source)
        return CollectionResult(source, errors=["RequestTimeout: unavailable"] if source == "coinbase" else [])

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", exchange)
    run_scheduler(settings, store, Once())
    states = Schedule(store, {"market": 3600, "liquidity": 300}).states()
    assert states[("coinbase", "market")]["next_due"] == now + 60
    assert states[("coinbase", "market")]["last_success"] == 0
    now += 30
    run_scheduler(settings, Store(settings.database), Once())
    assert sorted(calls) == ["coinbase", "kraken"]
    now += 31
    run_scheduler(settings, Store(settings.database), Once())
    assert calls.count("coinbase") == 2 and calls.count("kraken") == 1
    failed = Schedule(store, {"market": 3600}).states()[("coinbase", "market")]
    assert failed["failures"] == 2 and failed["next_due"] == now + 120


def test_success_and_permanent_failure_have_distinct_durable_clocks(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    schedule = Schedule(store, {"market": 3600})
    schedule.record({"source": "kraken", "errors": []}, "market", 100)
    schedule.record({"source": "kraken", "errors": ["HTTP 403"]}, "market", 200)
    row = Schedule(Store(store.path), {"market": 3600}).states()[("kraken", "market")]
    assert (row["last_attempt"], row["last_success"], row["failures"]) == (200, 100, 1)
    assert row["next_due"] == 200 + 21600 and row["retry_kind"] == "permanent_or_unknown"


@pytest.mark.parametrize("header", ["7200", datetime.fromtimestamp(7200, UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")])
def test_retry_after_deadline_never_sleeps_or_retries_early(monkeypatch, tmp_path, header):
    monkeypatch.setattr("crypto_market_analysis.providers.time.time", lambda: 0)
    sleeps = []
    monkeypatch.setattr("crypto_market_analysis.providers.time.sleep", lambda delay: sleeps.append(delay))
    requests = []
    client = PublicHTTP.__new__(PublicHTTP)

    class HTTP:
        def get(self, url, params=None):
            requests.append(url)
            return httpx.Response(429, headers={"Retry-After": header}, request=httpx.Request("GET", url))

    client.client = HTTP()
    for _ in range(2):
        with pytest.raises(RetryDeferred) as raised:
            client.get_json("https://example.com/data")
        assert raised.value.retry_at == 7200
    assert len(requests) == 1 and sleeps == []
    schedule = Schedule(Store(tmp_path / "retry.sqlite"), {"context": 86400})
    schedule.record({"source": "fred", "errors": [f"RetryDeferred: {raised.value}"]}, "context", 0)
    assert schedule.states()[("fred", "context")]["next_due"] == 7200


def test_context_retry_only_requests_selected_source():
    class Client:
        def __init__(self):
            self.urls = []

        def get_json(self, url, params=None):
            self.urls.append(url)
            return []

    client = Client()
    results = collect_context(now=1_800_000_000, client=client, sources=("defillama",))
    assert [r.source for r in results] == ["defillama"]
    assert client.urls == ["https://stablecoins.llama.fi/stablecoincharts/all"]


def test_interrupted_attempt_has_restart_cooldown_without_losing_success(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    schedule = Schedule(store, {"market": 3600})
    schedule.record({"source": "kraken", "errors": []}, "market", 100)
    schedule.start("kraken", "market", 4000)
    row = Schedule(Store(store.path), {"market": 3600}).states()[("kraken", "market")]
    assert row["last_attempt"] == 4000 and row["last_success"] == 100
    assert row["next_due"] == 4060 and row["retry_kind"] == "in_progress"


def test_startup_reconciles_completed_run_after_interrupted_state_update(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    schedule = Schedule(store, {"market": 3600})
    schedule.start("kraken", "market", 100)
    store.record_run(CollectionResult("kraken"), "market", 110)
    row = Schedule(Store(store.path), {"market": 3600}).states()[("kraken", "market")]
    assert row["last_success"] == 110 and row["next_due"] == 3710


def test_context_source_cadences_are_independent(monkeypatch, tmp_path):
    now = 1_800_000_000
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken"], options=False)
    store = Store(settings.database)
    for source, task in [("kraken", "market+liquidity"), ("storage", "retention"),
                         ("fred", "context"), ("coinmetrics", "context")]:
        store.record_run(CollectionResult(source), task, now)
    schedule = Schedule(store, {"context": 86400})
    schedule.record({"source": "defillama", "errors": ["RequestTimeout"]}, "context", now - 61)
    requested = []

    def context(**kwargs):
        requested.append(kwargs["sources"])
        return [CollectionResult(source) for source in kwargs["sources"]]

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_context", context)
    run_scheduler(settings, store, Once())
    assert requested == [("defillama",)]
    run_scheduler(settings, Store(settings.database), Once())
    assert requested == [("defillama",)]


@pytest.mark.parametrize("mode", ["demo", "import"])
def test_known_nonlive_history_does_not_reset_live_scheduler(monkeypatch, tmp_path, mode):
    now = 1_800_000_000
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken"], options=False, context=False)
    store = Store(settings.database)
    # Old live collection is due. A recent synthetic/import run cannot hide it.
    store.record_run(CollectionResult("kraken"), "market+liquidity", now - 86400, data_mode="live")
    store.record_run(CollectionResult("kraken"), "market+liquidity", now, data_mode=mode)
    store.record_run(CollectionResult("storage"), "retention", now)
    calls = []
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", lambda source, *a, **kw:
                        calls.append(source) or CollectionResult(source))
    run_scheduler(settings, store, Once())
    assert calls == ["kraken", "kraken"]
    assert store.scheduler_runs()[0]["data_mode"] in (None, "live")


def test_public_empty_and_failed_attempts_are_classified_live(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path, venues=["kraken", "coinbase"], options=False, context=False)
    store = Store(settings.database)
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", lambda source, *a, **kw:
                        CollectionResult(source, errors=["RequestTimeout"] if source == "coinbase" else []))
    report = collect(settings, store, tasks=("market",))
    assert {row["data_mode"] for row in report["reports"]} == {"live"}
    assert {row["status"] for row in report["reports"]} == {"empty", "error"}


def test_run_mode_infers_unique_evidence_and_preserves_unclassified_records(tmp_path):
    from dataclasses import replace

    store = Store(tmp_path / "modes.sqlite")
    candle = Candle("kraken", "BTC", "EUR", "1h", 0, 3600, 1, 1, 1, 1, 1, 3600, data_mode="demo")
    assert store.record_run(CollectionResult("kraken", candles=[candle]), "market", 3600)["data_mode"] == "demo"
    assert store.save_result(CollectionResult("kraken", candles=[candle]), "market", 3600)["data_mode"] == "demo"
    mixed = CollectionResult("kraken", candles=[candle, replace(candle, data_mode="import")])
    assert store.record_run(mixed, "market", 3600)["data_mode"] is None
    assert store.record_run(CollectionResult("kraken"), "retention", 3600)["data_mode"] is None
    assert store.record_run(CollectionResult("kraken", books=[{"data_mode": "import"}]),
                            "book_capture", 3600)["data_mode"] == "import"
    with pytest.raises(ValueError, match="conflicts"):
        store.save_result(CollectionResult("kraken", candles=[candle]), "market", 3600, data_mode="live")
    assert store.counts()["candles"] == 1


@pytest.mark.parametrize("errors", [[], ["RequestTimeout"]])
def test_public_book_empty_and_failed_attempts_are_classified_live(monkeypatch, tmp_path, errors):
    from crypto_market_analysis.collectors import capture_books

    store = Store(tmp_path / "books.sqlite")
    monkeypatch.setattr("crypto_market_analysis.collectors.collect_exchange", lambda source, *a, **kw:
                        CollectionResult(source, errors=errors))
    capture_books(store, sources=["kraken"], assets=["BTC"], quotes=("EUR",), clients=object())
    assert store.runs()[0]["data_mode"] == "live"


def test_scheduler_book_failure_does_not_retry_successful_market_after_restart(monkeypatch, tmp_path):
    from ccxt import RequestTimeout

    now = 1_800_000_000
    opened = now - 7200
    monkeypatch.setattr("crypto_market_analysis.pipeline.time.time", lambda: now)
    settings = Settings(data_dir=tmp_path, venues=["kraken"], assets=["BTC"], quote_currencies=["EUR"],
                        options=False, context=False, max_pages=1)
    store = Store(settings.database)
    store.record_run(CollectionResult("storage"), "retention", now)
    calls = []

    class PublicExchange:
        has = {"fetchOHLCV": True, "fetchOrderBook": True}

        def load_markets(self):
            return {"BTC/EUR": {"base": "BTC", "quote": "EUR", "symbol": "BTC/EUR", "spot": True}}

        def fetch_ohlcv(self, symbol, timeframe, *, since, limit):
            calls.append("candles")
            return [[opened * 1000, 100, 101, 99, 100, 1]]

        def fetch_order_book(self, symbol, *, limit):
            calls.append("book")
            raise RequestTimeout("Order-book endpoint unavailable")

    exchange = PublicExchange()

    def clients():
        pool = ExchangeClients()
        pool._clients["kraken"] = exchange
        return pool

    monkeypatch.setattr("crypto_market_analysis.pipeline.ExchangeClients", clients)
    run_scheduler(settings, store, Once())
    states = Schedule(store, {"market": settings.market_interval, "liquidity": settings.liquidity_interval}).states()
    assert states[("kraken", "market")]["last_success"] == now
    assert states[("kraken", "market")]["failures"] == 0
    assert states[("kraken", "market")]["next_due"] == now + 3600
    assert states[("kraken", "liquidity")]["last_success"] == 0
    assert states[("kraken", "liquidity")]["failures"] == 1
    assert states[("kraken", "liquidity")]["next_due"] == now + 60
    assert len(store.candles("BTC", source="kraken", quote="EUR")) == 1
    assert sorted(calls) == ["book", "candles"]
    now += 61
    run_scheduler(settings, Store(settings.database), Once())
    assert calls.count("candles") == 1 and calls.count("book") == 2
    reports = {row["task"]: row for row in store.runs() if row["source"] == "kraken"}
    assert reports["market"]["status"] == "ok" and reports["market"]["errors"] == []
    assert reports["liquidity"]["status"] == "error"


def test_one_shot_collect_keeps_combined_market_liquidity_report(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path, venues=["kraken"], assets=["BTC"], quote_currencies=["EUR"])
    requested = []
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", lambda source, *a, **kw:
                        requested.append(kw["include"]) or CollectionResult(source))
    report = collect(settings, Store(settings.database), tasks=("market", "liquidity"))
    assert requested == [("candles", "derivatives", "liquidity")]
    assert len(report["reports"]) == 1
    assert report["reports"][0]["task"] == "market+liquidity"
