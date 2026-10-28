"""Public capture regressions with deliberately reduced budgets, not scale claims."""

from dataclasses import replace

import pytest

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.evidence import (
    MAX_EVIDENCE_BYTES,
    archive_evidence,
    content_hash,
    estimated_evidence_bytes,
    restore_evidence,
)
from crypto_market_analysis.models import Candle, CollectionResult, Observation
from crypto_market_analysis.storage import Store
from crypto_market_analysis.vigilance import assess_vigilance

DAY = 86400
NOW = 800 * DAY


def candle(hour, source="wanted", quote="EUR"):
    opening = hour * 3600
    return Candle(source, "BTC", quote, "1h", opening, opening + 3600,
                  100, 100, 100, 100, 1, opening + 3600)


def book(event, metric="depth_bid_1pct", source="wanted", quote="EUR"):
    return Observation(source, "BTC", metric, event, event, 100, "bps" if metric == "spread_bps" else quote,
                       quote, {"coverage_complete": True, "symbol": f"BTC/{quote}", "timestamp_source": "exchange"})


def family(capture, name):
    return next(row for row in capture["truncation"]["observation_coverage"] if row["family"] == name)


def test_selected_market_filters_precede_real_store_cap_and_engine_selection(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=tmp_path))
    engine.store.put_candles([candle(0), candle(1)])
    original_capture = engine.store.capture

    def bounded_capture(*args, **kwargs):
        return original_capture(*args, candle_limit=2, **kwargs)

    monkeypatch.setattr(engine.store, "capture", bounded_capture)
    before = engine.capture_inputs("BTC", "wanted", "EUR", 20 * 3600, profile="candles")
    engine.store.put_candles([candle(hour, source, quote) for hour in range(2, 10)
                              for source, quote in (("other", "EUR"), ("wanted", "USD"))])
    after = engine.capture_inputs("BTC", "wanted", "EUR", 20 * 3600, profile="candles")
    assert before.candles == after.candles == (candle(0), candle(1))
    assert after.truncation["candles"] is False
    assert len(after.truncation["candle_coverage"]) == 1
    direct = original_capture("BTC", 20 * 3600, source="wanted", quote="EUR", candle_limit=2)
    assert direct["candles"] == [candle(0), candle(1)]
    with pytest.raises(ValueError, match="candle evidence exceeds the 1-row"):
        original_capture("BTC", 20 * 3600, source="wanted", quote="EUR", profile="candles", candle_limit=1)


def test_slow_macro_survives_book_load_with_true_age_and_honest_coverage(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    macro = Observation("fred", "MACRO", "DGS10", NOW - 30 * DAY, NOW - 1, 4, "percent")
    engine.store.put_observations([macro, *(book(NOW - offset) for offset in range(600))])
    raw = engine.store.capture("BTC", NOW, observation_limit=3)
    assert macro in raw["observations"]
    assert family(raw, "macro")["captured"] == 1
    assert family(raw, "liquidity")["available"] == 600
    assert family(raw, "liquidity")["captured"] == 2
    assert raw["truncation"]["observations"] is True
    point = next(row for row in engine.bundle(as_of=NOW, include_forecast=False)["latest_metrics"]
                 if row["metric"] == "DGS10")
    assert point["age_seconds"] == 30 * DAY
    assert point["observed_at"] == NOW - 1
    with pytest.raises(ValueError, match="Required bundle observation evidence"):
        engine.store.capture("BTC", NOW, observation_limit=2)


def test_bounded_bundle_retains_actual_native_comparisons_and_versions(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    # More than 256 points forces history sampling, but not the consumed 7/21d comparisons.
    rows = [replace(book(NOW - hour * 3600), value=float(hour + 1)) for hour in range(22 * 24)]
    engine.store.put_observations(rows)
    engine.store.put_observations([replace(rows[168], value=200, observed_at=NOW + 1),
                                   replace(rows[0], quote="USD", unit="USD"),
                                   replace(rows[0], data_mode="demo")])
    captured = engine.capture_inputs(as_of=NOW)
    assert len(captured.observations) == 258  # 256 recent, 21d comparison, separate USD identity.
    assert rows[168] in captured.observations
    assert rows[504] in captured.observations
    assert all(row.observed_at <= NOW and row.data_mode == "live" for row in captured.observations)
    summary = next(row for row in engine.bundle(captured=captured, include_forecast=False)["analysis"]
                   ["evidence"]["observations"] if row["quote"] == "EUR")
    assert summary["changes"]["7"]["absolute"] == -168
    assert summary["changes"]["21"]["absolute"] == -504
    assert summary["coverage"]["observations"] == 257
    assert captured.truncation["observations"] is True


def test_candle_profile_ignores_books_and_optional_context_and_stays_archivable(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=tmp_path))
    rows = [candle(hour) for hour in range((NOW // 3600) - 730 * 24, NOW // 3600)]
    engine.store.put_candles(rows)
    engine.store.put_observations([book(NOW - offset) for offset in range(1200)])
    engine.store.put_events([{"title": "Synthetic optional event", "event_time": NOW + 1,
                             "source_url": "https://example.org/event"}], NOW)
    # Ensure candle-only operations do not query/copy a global observation envelope.
    original = engine.store._capture_observations

    def check_profile(con, asset, cutoff, mode, profile, *args):
        assert profile == "candles"
        return original(con, asset, cutoff, mode, profile, *args)

    monkeypatch.setattr(engine.store, "_capture_observations", check_profile)
    captured = engine.capture_inputs("BTC", "wanted", "EUR", NOW, profile="candles")
    assert captured.candles == tuple(rows)
    assert captured.observations == captured.events == captured.calendar == ()
    assert captured.source_health["status"] == "not_requested"
    assert captured.capture_profile["name"] == "candles"
    evidence = captured.evidence()
    assert evidence["capture_profile"]["optional_context"] == "not_requested"
    assert captured.estimated_evidence_bytes == estimated_evidence_bytes(evidence) < MAX_EVIDENCE_BYTES
    assert captured.reference["content_hash"] == content_hash(evidence)
    assert restore_evidence(archive_evidence(evidence)) == evidence
    with pytest.raises(ValueError, match="32 MiB"):
        archive_evidence({"required": "x" * MAX_EVIDENCE_BYTES})


def test_vigilance_capture_keeps_full_custom_raw_baseline_and_native_prior(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    engine.store.put_candles([candle(hour) for hour in range(NOW // 3600 - 100, NOW // 3600)])
    rows = [book(NOW + offset, metric) for metric in ("spread_bps", "depth_bid_1pct", "depth_ask_1pct")
            for offset in range(-7200, 1, 300)]
    native = Observation("derivatives", "BTC", "open_interest_amount", NOW, NOW, 10, "BTC", "EUR",
                         {"symbol": "BTC/EUR:EUR"})
    engine.store.put_observations([*rows, native, replace(native, event_time=NOW - DAY, observed_at=NOW - DAY, value=9),
                                   replace(rows[0], metadata={"aggregation": "hourly_median"}),
                                   book(NOW, source="other"), book(NOW, quote="USD")])
    policy = {"baseline_seconds": 7200}
    captured = engine.capture_inputs("BTC", "wanted", "EUR", NOW, profile="vigilance", vigilance_policy=policy)
    assert len(captured.candles) == 48 <= 20000
    assert len(captured.observations) == len(rows) + 2 <= 100000
    result = assess_vigilance(list(captured.candles), list(captured.observations), NOW, policy=policy)
    assert result["quality"] == "sufficient"
    assert result["liquidity"]["spread_bps"]["baseline_samples"] == 24
    assert result["derivatives"][0]["native_change"] == 1
    with pytest.raises(ValueError, match="Required vigilance observation evidence exceeds the 20-row"):
        engine.store.capture("BTC", NOW, source="wanted", quote="EUR", profile="vigilance",
                             vigilance_policy=policy, observation_limit=20)


def test_profile_metadata_default_preserves_legacy_evidence_and_french_errors_are_english(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    captured = engine.capture_inputs(as_of=NOW)
    legacy = replace(captured, capture_profile=None)
    assert "capture_profile" not in legacy.evidence()
    assert legacy.reference["content_hash"] == content_hash(legacy.evidence())
    assert captured.reference["content_hash"] != legacy.reference["content_hash"]
    engine.dataset_lineage = {"dataset_id": "synthetic_research", "data_mode": "import"}
    lineage_capture = engine.capture_inputs(as_of=NOW)
    assert lineage_capture.reference["dataset_lineage"] == engine.dataset_lineage
    assert lineage_capture.evidence()["capture_profile"]["dataset_lineage"] == engine.dataset_lineage
    assert lineage_capture.reference["content_hash"] != captured.reference["content_hash"]
    store = Store(tmp_path / "invalid.sqlite")
    for invalid, message in ((replace(candle(0), close=float("nan")), "finite"),
                             (replace(candle(0), volume=-1), "nonnegative")):
        with pytest.raises(ValueError, match=message):
            store.put_candles([invalid])
    result = store.save_result(CollectionResult("fixture", candles=[replace(candle(0), volume=-1)],
                                               observations=[replace(book(100), value=float("nan"))]), "market", NOW)
    assert result["errors"][0].startswith("Candle validation:")
    assert result["errors"][1].startswith("Observation validation:")


def test_capture_watermark_counts_and_inputs_share_one_read_transaction(tmp_path, monkeypatch):
    store = Store(tmp_path / "coherent.sqlite")
    store.put_candles([candle(0)])
    initial = book(100)
    store.put_observations([initial])
    writer = Store(store.path)
    original = store._capture_observations
    later = replace(initial, event_time=200, observed_at=200, value=200)

    def concurrent_write(*args):
        writer.put_candles([candle(1)])
        writer.put_observations([later])
        return original(*args)

    monkeypatch.setattr(store, "_capture_observations", concurrent_write)
    captured = store.capture("BTC", DAY)
    assert captured["candles"] == [candle(0)]
    assert captured["observations"] == [initial]
    assert captured["watermark"]["candles"] == captured["counts"]["candles"] == 1
    assert captured["watermark"]["observations"] == captured["counts"]["observations"] == 1
    assert writer.counts()["observations"] == 2
