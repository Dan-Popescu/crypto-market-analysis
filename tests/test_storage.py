from dataclasses import replace

import pytest

from crypto_market_analysis.models import Candle, CollectionResult, Observation
from crypto_market_analysis.storage import Store


def candle():
    return Candle("test", "BTC", "USD", "1h", 0, 3600, 100, 105, 95, 102, 1, 7200)


def test_known_at_revisions_reversions_and_idempotence(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    first = candle()
    assert store.put_candles([first, first]) == 1
    assert store.put_candles([replace(first, observed_at=8000)]) == 0
    assert store.candles("BTC", as_of=7199) == []
    revision = replace(first, close=103, observed_at=9000)
    assert store.put_candles([revision]) == 1
    assert store.candles("BTC", as_of=8500)[0].close == 102
    assert store.candles("BTC", as_of=9500)[0].close == 103
    assert store.put_candles([replace(first, observed_at=10000)]) == 1
    assert store.candles("BTC", as_of=11000)[0].close == 102


def test_quote_mode_and_snapshot_watermarks_remain_separate(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    first = candle()
    store.put_candles([first, replace(first, quote="USDT"), replace(first, data_mode="demo")])
    watermark = store.watermark()
    store.put_candles([replace(first, close=103, observed_at=9000)])
    assert len(store.candles("BTC", as_of=10000)) == 2
    assert len(store.candles("BTC", quote="USD", mode="demo", as_of=10000)) == 1
    assert store.candles("BTC", quote="USD", as_of=10000, max_id=watermark["candles"])[0].close == 102


def test_observation_expiries_and_revisions_are_not_overwritten(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    first = Observation(
        "deribit", "BTC", "option_open_interest_expiry", 100, 200, 1, "BTC", "BTC", {"expiry": 1000}
    )
    other = replace(first, value=2, metadata={"expiry": 2000})
    assert store.put_observations([first, other, first]) == 2
    assert len(store.observations("BTC", as_of=250)) == 2
    assert store.put_observations([replace(first, value=3, observed_at=300)]) == 1
    assert sorted(o.value for o in store.observations("BTC", as_of=250)) == [1, 2]
    assert sorted(o.value for o in store.observations("BTC", as_of=350)) == [2, 3]


def test_invalid_batch_is_atomic(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    with pytest.raises(ValueError, match="open candle"):
        store.put_candles([candle(), replace(candle(), observed_at=1000)])
    assert store.counts()["candles"] == 0


def test_export_preserves_units_quotes_and_modes(tmp_path):
    import duckdb

    store = Store(tmp_path / "state.sqlite")
    store.put_candles([candle(), replace(candle(), quote="EUR", data_mode="demo")])
    result = store.export_parquet(tmp_path / "parquet")
    con = duckdb.connect()
    rows = con.execute(
        "SELECT quote,data_mode FROM read_parquet(?) ORDER BY quote", [result["candles"]["path"]]
    ).fetchall()
    assert rows == [("EUR", "demo"), ("USD", "live")]
    con.close()


def test_partial_validation_reports_what_was_actually_persisted(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    invalid = Observation("test", "BTC", "funding", 300, 200, 0.1, "rate")
    result = store.save_result(
        CollectionResult("test", candles=[candle()], observations=[invalid]), "market", 10000
    )
    assert result["status"] == "partial"
    assert result["inserted_candles"] == 1
    assert result["inserted_observations"] == 0
    assert result["candles"] == 1 and result["observations"] == 0
    assert store.counts()["candles"] == 1
