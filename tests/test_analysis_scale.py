"""Compact evidence preserves exact calculations under the existing budgets."""

import os
from dataclasses import replace

import pytest

from crypto_market_analysis import research
from crypto_market_analysis.analysis_dossier import MAX_RESULT_BYTES, build_analysis_dossier
from crypto_market_analysis.analysis_readiness import build_analysis_readiness
from crypto_market_analysis.evidence import canonical_json, content_hash
from crypto_market_analysis.models import Candle
from crypto_market_analysis.purchase_windows import analyze_windows
from crypto_market_analysis.storage import Store

NOW = 2000 * 86400


def history(days=100, asset="BTC", quote="EUR"):
    return [Candle("native", asset, quote, "1h", t, t + 3600,
                   100, 102, 98, 101, 1, t + 3600, data_mode="import")
            for t in range(NOW - days * 86400, NOW, 3600)]


def selection(asset="BTC", quote="EUR"):
    return {"asset": asset, "quote": quote, "source": "native", "lookbacks": [2, 30]}


def test_reference_dossier_preserves_calculations_and_exact_input_digest(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.put_candles(history())
    request = {"data_mode": "import", "markets": [selection()]}
    full = build_analysis_dossier(store, request, NOW)
    compact = build_analysis_dossier(store, {**request, "evidence_mode": "references"}, NOW)
    a = full["markets"][0]["market_context"]
    b = compact["markets"][0]["market_context"]
    assert {k: v for k, v in a.items() if k != "stored_inputs"} == {
        k: v for k, v in b.items() if k != "stored_inputs"}
    evidence = b["stored_inputs"]
    assert evidence["input_hash"] == a["stored_inputs"]["input_hash"]
    assert "consumed_records" not in evidence and "input_references" not in evidence
    assert evidence["manifest"]["consumed_records"] == 2400
    assert evidence["manifest"]["omitted_input_references"] == 2400
    assert evidence["manifest"]["retained_records"] == 0
    assert compact["input_hash"] == content_hash({k: v for k, v in compact.items()
                                                 if k not in ("input_hash", "snapshot_id")})
    assert len(canonical_json(compact)) < len(canonical_json(full)) // 5


def test_reference_readiness_reuses_exact_candles_for_windows(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    rows = history()
    store.put_candles(rows)
    request = {"dossier": {"data_mode": "import", "markets": [selection()]},
               "windows": {"horizons": [1, 7], "thresholds": [0.02, 0.05]},
               "qualification": {"required_history_days": 100}}
    full = build_analysis_readiness(store, request, NOW)
    from crypto_market_analysis import analysis_dossier
    encode = analysis_dossier.canonical_json

    def guard(value):
        # Reference mode must never serialize a complete raw dossier first.
        if isinstance(value, dict) and "resource_limits" in value and "markets" in value:
            for market in value["markets"]:
                assert "consumed_records" not in market["market_context"]["stored_inputs"]
        return encode(value)

    monkeypatch.setattr(analysis_dossier, "canonical_json", guard)
    compact = build_analysis_readiness(store, {**request, "evidence_mode": "references"}, NOW)
    assert compact["markets"] == full["markets"]
    assert compact["markets"][0]["windows"] == analyze_windows(rows, NOW, **request["windows"])


def test_four_dense_800_day_readiness_fits_unchanged_limits(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    markets = [selection(asset, quote) for asset in ("BTC", "ETH") for quote in ("EUR", "USD")]
    store.put_candles([row for market in markets for row in history(800, market["asset"], market["quote"])])
    result = build_analysis_readiness(store, {
        "dossier": {"data_mode": "import", "markets": markets},
        "evidence_mode": "references", "qualification": {"required_history_days": 800}}, NOW)
    assert len(canonical_json(result).encode()) < MAX_RESULT_BYTES
    assert result["dossier"]["resource_limits"]["sqlite_vm_steps"] == 50_000_000
    assert result["dossier"]["resource_limits"]["sqlite_returned_bytes"] == 64 * 1024 * 1024
    assert result["dossier"]["resource_limits"]["read_session_seconds"] == 15
    assert all(m["market_context"]["stored_inputs"]["manifest"]["consumed_records"] == 19200
               for m in result["dossier"]["markets"])
    assert all(m["coverage"]["missing_hours"] == 0 for m in result["qualification"]["markets"])


def test_grouped_latest_times_keep_native_source_mode_and_missingness(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assets = [f"COIN{i}" for i in range(1000)]
    base = history(1)[-1]
    later = replace(base, asset=assets[0], open_time=NOW, close_time=NOW + 3600, observed_at=NOW + 3600)
    store.put_candles([replace(base, asset=asset) for asset in assets]
                      + [replace(later, source="elsewhere"), replace(later, data_mode="demo"),
                         replace(later, quote="USD")])
    with store.read_session() as session:
        result = session.latest_candle_times("native", assets, quotes=["EUR"], mode="import")
    assert result == {(asset, "EUR"): NOW - 3600 for asset in assets}
    assert store.latest_candle_times("missing", assets, quotes=["EUR"], mode="import") == {}
    with pytest.raises(ValueError):
        store.latest_candle_times("native", assets + ["EXTRA"], quotes=["EUR"])


def test_threshold_preparation_reuses_bars_features_and_matches_original_estimator(monkeypatch):
    rows = []
    for i, row in enumerate(history()):
        if i == 72 * 24:
            continue
        value = 100 + (i % 37) / 7 + (i // 24) * 0.25
        rows.append(replace(row, open=value, close=value * 1.001, high=value * 1.01, low=value * 0.99))
    expected = {}
    bars = research.daily_bars(rows)
    for threshold in (0.02, 0.05):
        expected[threshold] = {str(h): research._estimate(bars, len(bars) - 1, h, threshold)
                               for h in (1, 7)}
    original = research._feature
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(research, "_feature", counted)
    scenarios = research.forecast_thresholds(rows, horizons=[1, 7], thresholds=[0.02, 0.05], as_of=NOW)
    assert calls == len(bars)
    for scenario in scenarios:
        assert scenario["horizons"] == expected[scenario["threshold"]]


def test_reference_results_rebuild_after_database_replacement_with_reused_rowids(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    replacement = Store(tmp_path / "replacement.sqlite")
    rows = history(3)
    store.put_candles(rows)
    replacement.put_candles([replace(row, open=200, high=202, low=198, close=201) for row in rows])
    request = {"data_mode": "import", "markets": [selection()], "evidence_mode": "references"}
    before = build_analysis_dossier(store, request, NOW)
    os.replace(replacement.path, store.path)
    after = build_analysis_dossier(store, request, NOW)
    assert before["watermark"] == after["watermark"]
    assert before["markets"][0]["market_context"]["anchor"]["price"] == 101
    assert after["markets"][0]["market_context"]["anchor"]["price"] == 201
    assert before["input_hash"] != after["input_hash"]
