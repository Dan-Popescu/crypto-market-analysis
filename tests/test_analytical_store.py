import json
from dataclasses import replace

import pytest

from crypto_market_analysis.analytical_store import (
    AnalyticalCandleStore,
    export_candle_snapshot,
    open_analytical_snapshot,
)
from crypto_market_analysis.models import Candle
from crypto_market_analysis.storage import Store

OPEN = 1_699_999_200


def fixture_store(tmp_path):
    store = Store(tmp_path / "market.sqlite")
    original = Candle("venue", "BTC", "EUR", "1h", OPEN, OPEN + 3600,
                      100., 104., 99., 101., 10., OPEN + 3600)
    store.put_candles([original, replace(original, quote="USD"), replace(original, source="other"),
                       replace(original, data_mode="demo"), replace(original, asset="ETH")])
    store.put_candles([replace(original, close=102., observed_at=OPEN + 7200)])
    # Equal receipt time must use the original SQLite row id to break ties.
    store.put_candles([replace(original, close=103., observed_at=OPEN + 7200)])
    return store, original


def test_snapshot_retains_known_at_and_native_dimensions(tmp_path):
    store, original = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    manifest = export_candle_snapshot(store, target, batch_rows=2)
    assert manifest["rows"] == 7
    assert manifest["sqlite_max_id"] == 7
    reader = AnalyticalCandleStore(target)
    bounds = {"start": OPEN, "end": OPEN + 3600, "source": "venue", "quote": "EUR"}
    assert reader.candles(["BTC"], as_of=OPEN + 3600, **bounds) == [original]
    assert reader.candles(["BTC"], as_of=OPEN + 7200, **bounds)[0].close == 103.
    assert reader.candles(["BTC"], as_of=OPEN + 7200, max_id=6, **bounds)[0].close == 102.
    assert reader.candles(["BTC"], as_of=OPEN + 7200, max_id=5, **bounds) == [original]
    assert len(reader.candles(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200)) == 3
    assert reader.candles(["BTC"], as_of=OPEN + 7200, mode="demo", **bounds) == [replace(original, data_mode="demo")]
    # A later SQLite receipt cannot leak into this frozen artifact.
    store.put_candles([replace(original, close=100., observed_at=OPEN + 10800)])
    assert reader.candles(["BTC"], as_of=OPEN + 10800, **bounds)[0].close == 103.
    for asset in ("BTC", "ETH"):
        for cutoff in (OPEN + 3600, OPEN + 7200):
            expected = store.candles(asset, source="venue", quote="EUR", as_of=cutoff)
            assert reader.candles([asset], as_of=cutoff, **bounds) == expected


def test_missing_empty_and_bounded_exports(tmp_path):
    absent = Store(tmp_path / "absent.sqlite", initialize=False)
    with pytest.raises(ValueError, match="missing"):
        export_candle_snapshot(absent, tmp_path / "missing")
    assert not absent.path.exists()
    assert not (tmp_path / "missing").exists()
    empty = Store(tmp_path / "empty.sqlite")
    export_candle_snapshot(empty, tmp_path / "empty")
    assert AnalyticalCandleStore(tmp_path / "empty").candles(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 3600) == []
    store, _ = fixture_store(tmp_path)
    with pytest.raises(ValueError, match="row budget"):
        export_candle_snapshot(store, tmp_path / "bounded", max_rows=1)
    assert not (tmp_path / "bounded").exists()
    assert not list(tmp_path.glob(".bounded-*"))
    export_candle_snapshot(store, tmp_path / "frozen")
    with pytest.raises(ValueError, match="new directory"):
        export_candle_snapshot(store, tmp_path / "frozen")


def test_corruption_missing_file_and_unsafe_manifest_rejected(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    path = target / "candles.parquet"
    original = path.read_bytes()
    path.write_bytes(original[:-1])
    with pytest.raises(ValueError, match="checksum"):
        AnalyticalCandleStore(target)
    path.write_bytes(original)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["file"] = "../other.parquet"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        AnalyticalCandleStore(target)
    manifest["file"] = "candles.parquet"
    manifest["content_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="source-content"):
        AnalyticalCandleStore(target)
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        AnalyticalCandleStore(target)


def test_query_fails_before_returning_truncated_results(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    reader = AnalyticalCandleStore(target)
    bounds = {"start": OPEN, "end": OPEN + 3600, "as_of": OPEN + 7200}
    with pytest.raises(ValueError, match="row budget"):
        reader.candles(["BTC"], max_rows=1, **bounds)
    with pytest.raises(ValueError, match="receipt fence"):
        reader.candles(["BTC"], max_id=8, **bounds)
    with pytest.raises(ValueError, match="bounds"):
        reader.candles(["BTC"], **{**bounds, "end": OPEN + 10800})
    with pytest.raises(ValueError, match="assets"):
        reader.candles([], **bounds)
    with pytest.raises(ValueError, match="between"):
        reader.candles(["BTC"], max_rows=True, **bounds)


def test_symlinked_snapshot_rejected(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        AnalyticalCandleStore(alias)


def test_cached_verification_reuses_only_unchanged_artifacts(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    first = open_analytical_snapshot(target)
    assert open_analytical_snapshot(target) is first
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] += 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="changed after verification"):
        first.candles(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200)
    second = open_analytical_snapshot(target)
    assert second is not first
    path = target / "candles.parquet"
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="checksum"):
        open_analytical_snapshot(target)


def test_fleet_summary_keeps_native_series_and_reports_unknown_assets(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    reader = AnalyticalCandleStore(target)
    result = reader.fleet_summary(["BTC", "ETH", "UNKNOWN"], start=OPEN, end=OPEN + 10800, as_of=OPEN + 10800)
    assert result["missing_assets"] == ["UNKNOWN"]
    assert result["series_count"] == 4
    assert result["selected_points"] == 4
    row = next(row for row in result["series"] if (row["asset"], row["source"], row["quote"]) == ("BTC", "venue", "EUR"))
    assert row["latest_close"] == 103.
    assert row["latest_observed_at"] == OPEN + 7200
    assert row["expected_hours"] == 3
    assert row["missing_hours"] == 2
    assert row["coverage_ratio"] == 1 / 3
    assert result["snapshot"]["content_sha256"] == reader.manifest["content_sha256"]
    assert result["expires_at"] is None
    earlier = reader.fleet_summary(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 3600, source="venue", quote="EUR")
    assert earlier["series"][0]["latest_close"] == 101.
    assert earlier["series"][0]["missing_hours"] == 0
    with pytest.raises(ValueError, match="native-series"):
        reader.fleet_summary(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200, max_series=1)
    with pytest.raises(ValueError, match="aligned"):
        reader.fleet_summary(["BTC"], start=OPEN + 1, end=OPEN + 3600, as_of=OPEN + 7200)
    with pytest.raises(ValueError, match="asset budget"):
        reader.fleet_summary(["BTC", "ETH"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200, max_assets=1)


@pytest.mark.parametrize("assets", [100, 1000])
def test_fleet_capacity_matches_independent_sqlite_python_selection(tmp_path, assets):
    # Synthetic storage capacity beyond the pre-migration BTC/ETH producer guard.
    # Every asset has two native quotes and an earlier and later receipt per bar.
    store = Store(tmp_path / "market.sqlite")
    symbols = [f"SYNTHETIC{number:04d}" for number in range(assets)]
    with store.connection() as con:
        for symbol in symbols:
            for quote in ("EUR", "USD"):
                original = Candle("synthetic", symbol, quote, "1h", OPEN, OPEN + 3600,
                                  100., 104., 99., 101., 10., OPEN + 3600, data_mode="demo")
                for candle in (original, replace(original, close=103., observed_at=OPEN + 7200)):
                    payload = json.dumps(candle.__dict__, sort_keys=True)
                    con.execute("INSERT INTO candles(source,asset,quote,timeframe,open_time,close_time,observed_at,data_mode,fingerprint,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                ("synthetic", symbol, quote, "1h", OPEN, OPEN + 3600, candle.observed_at, "demo", payload, payload))
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    result = AnalyticalCandleStore(target).fleet_summary(symbols, start=OPEN, end=OPEN + 3600,
                                                       as_of=OPEN + 3600, mode="demo")
    with store.connection() as con:
        # Independent Python receipt selection from actual SQLite rows.
        selected = {}
        for row in con.execute("SELECT * FROM candles ORDER BY observed_at,id"):
            if row["observed_at"] <= OPEN + 3600 and row["close_time"] <= OPEN + 3600:
                selected[row["asset"], row["quote"], row["open_time"]] = json.loads(row["payload"])
    expected = [(asset, quote, candle["close"], candle["observed_at"])
                for (asset, quote, _), candle in sorted(selected.items())]
    actual = [(row["asset"], row["quote"], row["latest_close"], row["latest_observed_at"]) for row in result["series"]]
    assert actual == expected
    assert result["series_count"] == 2 * assets
    assert result["selected_points"] == 2 * assets
    assert all(row["missing_hours"] == 0 for row in result["series"])
