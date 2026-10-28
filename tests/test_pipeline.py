import time
from threading import Event

from crypto_market_analysis.config import Settings
from crypto_market_analysis.models import Candle, CollectionResult
from crypto_market_analysis.pipeline import collect, run_scheduler
from crypto_market_analysis.storage import Store


def test_source_failure_does_not_delete_other_sources(monkeypatch, tmp_path):
    now = int(time.time())
    opened = now // 3600 * 3600 - 3600
    good = Candle("kraken", "BTC", "USD", "1h", opened, opened + 3600, 100, 101, 99, 100, 1, now)

    def fake(venue, *args, **kwargs):
        return (
            CollectionResult(venue, candles=[good])
            if venue == "kraken"
            else CollectionResult(venue, errors=["HTTP 451"])
        )

    monkeypatch.setattr("crypto_market_analysis.pipeline.collect_exchange", fake)
    settings = Settings(data_dir=tmp_path, venues=["kraken", "binance"])
    store = Store(settings.database)
    result = collect(settings, store, tasks=("market",))
    assert result["status"] == "partial"
    assert len(store.candles("BTC")) == 1
    assert {r["status"] for r in result["reports"]} == {"ok", "error"}
    second = collect(settings, store, tasks=("market",))
    assert sum(r["inserted_candles"] for r in second["reports"]) == 0
    assert len(store.candles("BTC")) == 1


def test_scheduler_restarts_without_repeating_fresh_jobs(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path, venues=["kraken"], options=False, context=False)
    store = Store(settings.database)
    now = int(time.time())
    store.record_run(CollectionResult("kraken"), "market+liquidity", now)
    store.record_run(CollectionResult("storage"), "retention", now)
    calls = []
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect", lambda *a, **kw: calls.append(kw))

    class Once(Event):
        def wait(self, timeout=None):
            self.set()
            return True

    run_scheduler(settings, store, Once())
    assert calls == []


def test_scheduler_catches_up_old_liquidity_backlog(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path, venues=["kraken"], options=False, context=False)
    store = Store(settings.database)
    calls = []
    pending = iter([100, 50, 0])

    def compact(*args, **kwargs):
        calls.append(True)
        return {"remaining_old_rows": next(pending), "aggregates_written": 1, "raw_rows_deleted": 10}

    monkeypatch.setattr("crypto_market_analysis.retention.compact_liquidity", compact)
    monkeypatch.setattr("crypto_market_analysis.pipeline.collect", lambda *a, **kw: {"status": "ok"})

    class Once(Event):
        def wait(self, timeout=None):
            self.set()
            return True

    run_scheduler(settings, store, Once())
    assert len(calls) == 3
    assert "pending_backlog" not in store.runs()[0]["warnings"]
