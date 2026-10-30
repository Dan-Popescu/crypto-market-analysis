from dataclasses import replace

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle, Observation


def seed(engine, now=20 * 86400 + 1800):
    rows = []
    for quote, price in (("USD", 100), ("EUR", 200)):
        for hour in range(24):
            opening = 19 * 86400 + hour * 3600
            rows.append(Candle("fixture", "BTC", quote, "1h", opening, opening + 3600,
                               price, price + 1, price - 1, price, 1, opening + 3600))
    engine.store.put_candles(rows)
    engine.store.put_observations([
        Observation("fixture", "BTC", "depth_bid_1pct", now - 1800, now - 1, 1000, "USD", "USD")
    ])
    return now, rows


def test_quote_and_configuration_are_part_of_input_identity(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    now, _ = seed(engine)
    usd = engine.bundle("BTC", "fixture", "USD", now, False)
    eur = engine.bundle("BTC", "fixture", "EUR", now, False)
    assert usd["snapshot_id"] != eur["snapshot_id"]
    assert usd["schema_version"] == "2.0"
    assert usd["input_reference"]["quote"] == "USD"
    engine.settings.liquidity_interval = 600
    changed = engine.bundle("BTC", "fixture", "USD", now, False)
    assert changed["snapshot_id"] != usd["snapshot_id"]


def test_old_liquidity_reingestion_does_not_refresh_event_age(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, liquidity_interval=300))
    now, _ = seed(engine)
    point = engine.bundle("BTC", "fixture", "USD", now, False)["latest_metrics"][0]
    assert point["age_seconds"] == 1800
    assert point["stale"] is True
    assert point["freshness"]["cadence_seconds"] == 300
    assert point["freshness"]["ingestion_age_seconds"] == 1


def test_captured_inputs_survive_later_writes_and_calendar_changes(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    now, rows = seed(engine)
    captured = engine.capture_inputs("BTC", "fixture", "USD", now)
    before = engine.bundle(captured=captured, include_forecast=False)
    engine.store.put_candles([replace(rows[23], close=100.5, observed_at=now)])
    engine.store.put_events([{"title": "Synthetic event", "event_time": now + 100,
                             "source_url": "https://example.org/synthetic"}], now)
    after = engine.bundle(captured=captured, include_forecast=False)
    assert after == before
    assert before["events"] == []
    assert before["analysis"]["evidence"]["latest_close"] == 100
    assert engine.bundle("BTC", "fixture", "USD", now, False)["events"]


def test_metric_periods_and_units_do_not_collapse(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    now, _ = seed(engine)
    base = Observation("fixture", "BTC", "funding_rate_actual", now - 1, now,
                       0.0001, "rate", "USD", {"funding_interval_hours": 1, "symbol": "BTC/USD"})
    engine.store.put_observations([base, replace(base, metadata={**base.metadata, "funding_interval_hours": 8}),
                                   replace(base, unit="percent")])
    points = engine.bundle("BTC", "fixture", "USD", now, False)["latest_metrics"]
    funding = [point for point in points if point["metric"] == "funding_rate_actual"]
    assert len(funding) == 3


def test_capture_is_as_of_and_mode_specific(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    now, rows = seed(engine)
    engine.store.put_candles([replace(rows[0], data_mode="demo")])
    captured = engine.capture_inputs("BTC", "fixture", "USD", now)
    assert all(row.data_mode == "live" for row in captured.candles)
    assert captured.source_health["counts"]["candles"] == 48
    assert all(row.close_time <= now and row.observed_at <= now for row in captured.candles)
