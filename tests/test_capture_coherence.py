from dataclasses import replace

import pytest

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.evidence import content_hash, restore_evidence
from crypto_market_analysis.models import Candle
from crypto_market_analysis.plans import compare_plans
from crypto_market_analysis.storage import Store


@pytest.mark.parametrize("recycle_ids", [False, True])
def test_issued_plan_and_windows_share_the_transaction_capture(tmp_path, monkeypatch, recycle_ids):
    issued = 4 * 86400
    engine = Engine(Settings(data_dir=tmp_path))
    rows = [Candle("fixture", "BTC", "EUR", "1h", t, t + 3600,
                   10, 10, 10, 10, 1, t + 3600)
            for t in range(issued - 48 * 3600, issued, 3600)]
    engine.store.put_candles(rows)
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "start_time": rows[1].open_time,
              "contributions": [{"time": rows[1].open_time, "amount": "100"}],
              "schedule": [{"time": rows[1].open_time, "amount": "100"}]}
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: issued)
    capture = engine.capture_inputs

    def capture_then_collect(*args, **kwargs):
        captured = capture(*args, **kwargs)
        assert captured.watermark["candles"] == 48
        # A separate collector commits revisions after the snapshot transaction.
        # Leave the anchor alone to avoid conflicting identical availability.
        collector = Store(engine.settings.database)
        revisions = [replace(row, open=20, high=20, low=20, close=20,
                             observed_at=issued) for row in rows[:-1]]
        if recycle_ids:
            # A physical watermark cannot identify deleted/replaced evidence.
            with collector.connection() as con:
                con.execute("DELETE FROM candles")
            revisions.append(rows[-1])
        collector.put_candles(revisions)
        return captured

    monkeypatch.setattr(engine, "capture_inputs", capture_then_collect)
    record = engine.issue_assessment("interleaved-collector", "BTC", "fixture", "EUR",
                                     plan_config=config)
    payload = record["payload"]
    regular = payload["plan"]["plans"]["regular"]
    assert regular["net_quantity"] == "10"
    assert regular["history"][0]["reference_open"] == "10"
    assert payload["windows"]["anchor_price"] == 10
    evidence = restore_evidence(record["evidence"])
    assert evidence["plan_candle_versions"] == []
    assert all(row["close"] == 10 for row in evidence["candles"])
    assert content_hash(evidence) == record["evidence"]["sha256"]
    assert payload["plan"]["input_reference"]["content_hash"] == content_hash(evidence["candles"])
    assert engine.store.watermark()["candles"] == (48 if recycle_ids else 95)
    assert payload["plan"]["input_reference"]["watermark"] == payload["input_reference"]["watermark"]
    # Later observation must replay the archived prices despite the conflicting
    # database history, and consume the first fully post-issuance vintage.
    future = replace(rows[-1], open_time=issued, close_time=issued + 3600,
                     observed_at=issued + 3600, open=30, high=30, low=30, close=30)
    engine.store.put_candles([future, replace(future, open=40, high=40, low=40, close=40,
                                            observed_at=issued + 7200)])
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: issued + 7200)
    observed = engine.observe_assessment(record["assessment_id"])["payload"]["plan"]
    assert observed["plans"]["regular"]["net_quantity"] == "10"
    assert observed["replay"]["pinned_content_hash"] == payload["plan"]["input_reference"]["content_hash"]
    assert observed["replay"]["future_content_hash"] == content_hash([future])
    assert engine.assessment(record["assessment_id"]) == record


def test_capture_preserves_timely_and_first_later_known_vintages(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=tmp_path))
    timely = Candle("fixture", "BTC", "EUR", "1h", 0, 3600, 10, 10, 10, 10, 1, 3600)
    first_late = replace(timely, open_time=3600, close_time=7200, observed_at=12000,
                         open=20, high=20, low=20, close=20)
    revisions = [replace(timely, open=40, high=40, low=40, close=40, observed_at=16000),
                 replace(first_late, open=30, high=30, low=30, close=30, observed_at=17000)]
    engine.store.put_candles([timely, first_late, *revisions])
    # These dimensions and future availability must never enter this replay.
    engine.store.put_candles([replace(timely, source="other"), replace(timely, quote="USD"),
                              replace(timely, asset="ETH"), replace(timely, data_mode="demo"),
                              replace(timely, open=50, high=50, low=50, close=50, observed_at=20000)])
    engine.dataset_lineage = {"name": "synthetic-history", "data_mode": "live"}
    ordinary = engine.capture_inputs("BTC", "fixture", "EUR", 18000, profile="candles")
    captured = engine.capture_inputs("BTC", "fixture", "EUR", 18000, profile="candles",
                                     include_candle_versions=True)
    assert captured.evidence() == ordinary.evidence()
    assert captured.reference == ordinary.reference
    assert ordinary.candle_versions is None
    assert captured.candle_versions == (timely, revisions[0], first_late, revisions[1])
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "start_time": 0, "contributions": [{"time": 0, "amount": "100"}],
              "schedule": [{"time": 0, "amount": "100"}]}
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: 18000)
    record = engine.issue_assessment("versioned-replay", "BTC", "fixture", "EUR", plan_config=config)
    evidence = restore_evidence(record["evidence"])
    assert [row["open"] for row in evidence["candles"]] == [40, 30]
    assert [row["open"] for row in evidence["plan_candle_versions"]] == [10, 20]
    assert content_hash(evidence) == record["evidence"]["sha256"]
    assert record["payload"]["plan"]["input_reference"]["content_hash"] == content_hash(captured.candle_versions)
    # The captured operation owns its lineage and prices after local maintenance.
    engine.dataset_lineage["name"] = "changed-after-capture"
    with engine.store.connection() as con:
        con.execute("DELETE FROM candles")
    normalized, cutoff, rows, reference = engine.accumulation._plan_inputs(config, 18000, captured=captured)
    assert reference["dataset_lineage"]["name"] == "synthetic-history"
    assert reference["available_at"] == 17000
    known = compare_plans(normalized, rows, cutoff, validation="known_at")
    retrospective = compare_plans(normalized, rows, cutoff)
    assert known["plans"]["regular"]["history"][0]["reference_open"] == "10"
    assert retrospective["plans"]["regular"]["history"][0]["reference_open"] == "40"
    assert min((row for row in rows if row.open_time == 3600), key=lambda row: row.observed_at) == first_late
    replay = engine.observe_assessment(record["assessment_id"])["payload"]["plan"]
    assert replay["replay"]["pinned_candle_versions"] == 4
    assert replay["replay"]["pinned_content_hash"] == content_hash(captured.candle_versions)
    assert replay["plans"]["regular"]["history"][0]["reference_open"] == "40"


def test_version_capture_refuses_partial_history_and_requires_a_native_market(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    first = Candle("fixture", "BTC", "EUR", "1h", 0, 3600, 10, 10, 10, 10, 1, 3600)
    engine.store.put_candles([first, replace(first, open=20, high=20, low=20, close=20, observed_at=4000)])
    # One current bar fits, but silently dropping a vintage would change replay.
    assert len(engine.store.capture("BTC", 4000, source="fixture", quote="EUR",
                                    profile="candles", candle_limit=1)["candles"]) == 1
    with pytest.raises(ValueError, match="candle versions exceed the 1-row capture limit"):
        engine.store.capture("BTC", 4000, source="fixture", quote="EUR", profile="candles",
                             candle_limit=1, include_candle_versions=True)
    with pytest.raises(ValueError, match="explicit source and native quote"):
        engine.capture_inputs(as_of=4000, include_candle_versions=True)


def test_plan_replay_rejects_a_different_or_unversioned_capture(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "start_time": 0, "contributions": [], "schedule": []}
    captured = engine.capture_inputs("BTC", "fixture", "EUR", 3600, profile="candles")
    with pytest.raises(ValueError, match="transaction-captured candle versions"):
        engine.accumulation._plan_inputs(config, 3600, captured=captured)
    with pytest.raises(ValueError, match="captured market, mode and cutoff"):
        engine.accumulation._plan_inputs({**config, "quote": "USD"}, 3600, captured=captured)


def test_versioned_evaluation_survives_removal_of_physical_rows(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    end = 90 * 86400
    engine.store.put_candles([Candle("fixture", "BTC", "EUR", "1h", t, t + 3600,
                                    100, 101, 99, 100, 1, t + 3600)
                              for t in range(0, end, 3600)])
    captured = engine.capture_inputs("BTC", "fixture", "EUR", end, profile="candles",
                                     include_candle_versions=True)
    expected = engine.evaluation("BTC", 1, 0.1, "fixture", "EUR", end, captured=captured)
    assert expected["origins"]
    with engine.store.connection() as con:
        con.execute("DELETE FROM candles")
    assert engine.evaluation("BTC", 1, 0.1, captured=captured) == expected
