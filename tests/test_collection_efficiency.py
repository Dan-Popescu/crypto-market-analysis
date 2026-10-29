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
    settings = Settings(data_dir=tmp_path, venues=["kraken", "coinbase"], options=False, context=False)
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
