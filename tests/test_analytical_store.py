import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import crypto_market_analysis.analytical_store as analytical_store
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


def test_cold_verification_has_its_own_deadline_before_any_sql(tmp_path, monkeypatch):
    from crypto_market_analysis import analytical_store

    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target)
    clock = [0]
    monkeypatch.setattr(analytical_store.time, "monotonic", lambda: clock[0])

    def slow_file_hash(path, check_budget):
        clock[0] = 31
        check_budget()
        pytest.fail("An expired cold verification must stop before a query.")

    monkeypatch.setattr(analytical_store, "_file_hash", slow_file_hash)
    with pytest.raises(ValueError, match="verification deadline"):
        AnalyticalCandleStore(target)


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


def test_partitioned_known_at_ties_match_filtered_v1_and_sqlite(tmp_path):
    store, original = fixture_store(tmp_path)
    filters = {"assets": ["BTC"], "source": "venue", "quote": "EUR", "mode": "live",
               "start": OPEN, "end": OPEN + 3600}
    first = export_candle_snapshot(store, tmp_path / "v1", max_rows=3, **filters)
    second = export_candle_snapshot(store, tmp_path / "v2", max_rows=3, rows_per_file=2, **filters)
    assert first["format"] == "observer-candle-snapshot-v1"
    assert second["format"] == "observer-candle-snapshot-v2"
    assert [part["rows"] for part in second["files"]] == [2, 1]
    assert first["content_sha256"] == second["content_sha256"]
    assert first["canonical_source_bytes"] == second["canonical_source_bytes"]
    assert second["selection"] == filters
    left = AnalyticalCandleStore(tmp_path / "v1")
    right = open_analytical_snapshot(tmp_path / "v2")
    assert open_analytical_snapshot(tmp_path / "v2") is right
    bounds = {"start": OPEN, "end": OPEN + 3600, "source": "venue", "quote": "EUR"}
    for cutoff in (OPEN + 3600, OPEN + 7200):
        for fence in (5, 6, 7):
            expected = store.candles("BTC", source="venue", quote="EUR", as_of=cutoff, max_id=fence)
            assert left.candles(["BTC"], as_of=cutoff, max_id=fence, **bounds) == expected
            assert right.candles(["BTC"], as_of=cutoff, max_id=fence, **bounds) == expected
    assert right.candles(["BTC"], as_of=OPEN + 3600, **bounds) == [original]
    fleet = right.fleet_summary(["BTC"], as_of=OPEN + 7200, **bounds)
    assert fleet["series"][0]["latest_close"] == 103.
    assert fleet["snapshot"]["selection"] == filters
    assert fleet["snapshot"]["partition_count"] == 2


def test_legacy_v1_and_partitioned_empty_artifacts_remain_readable(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "legacy"
    manifest = export_candle_snapshot(store, target)
    for key in ("selection", "export_budget", "canonical_source_bytes"):
        manifest.pop(key)
    (target / "manifest.json").write_text(json.dumps(manifest))
    result = AnalyticalCandleStore(target).fleet_summary(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200)
    assert result["snapshot"]["selection"] is None
    empty = Store(tmp_path / "empty.sqlite")
    manifest = export_candle_snapshot(empty, tmp_path / "empty", max_rows=100_000_000, rows_per_file=1_000_000)
    assert manifest["rows"] == 0
    assert len(manifest["files"]) == 1
    assert AnalyticalCandleStore(tmp_path / "empty").candles(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 3600) == []
    with pytest.raises(ValueError, match="partitioned output"):
        export_candle_snapshot(empty, tmp_path / "too-big-v1", max_rows=10_000_001)
    with pytest.raises(ValueError, match="max_rows"):
        export_candle_snapshot(empty, tmp_path / "too-big-v2", max_rows=100_000_001, rows_per_file=1_000_000)


def test_warmed_partition_cache_checks_files_without_rehashing_rows(tmp_path, monkeypatch):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target, rows_per_file=4)
    reader = open_analytical_snapshot(target)

    def unexpected_hash(*args, **kwargs):
        raise AssertionError("Warmed immutable artifact must not be fully hashed again")

    monkeypatch.setattr(analytical_store, "_file_hash", unexpected_hash)
    assert open_analytical_snapshot(target) is reader
    assert reader.fleet_summary(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200)["selected_points"] == 3


def test_partition_corruption_and_unsafe_file_list_fail_closed(tmp_path):
    store, _ = fixture_store(tmp_path)
    target = tmp_path / "snapshot"
    export_candle_snapshot(store, target, rows_per_file=4)
    reader = open_analytical_snapshot(target)
    part = target / "candles-000002.parquet"
    original = part.read_bytes()
    part.write_bytes(original[:-1])
    with pytest.raises(ValueError, match="changed after verification"):
        reader.fleet_summary(["BTC"], start=OPEN, end=OPEN + 3600, as_of=OPEN + 7200)
    with pytest.raises(ValueError, match="checksum"):
        open_analytical_snapshot(target)
    part.write_bytes(original)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][1]["file"] = "../other.parquet"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe file"):
        AnalyticalCandleStore(target)
    part.unlink()
    manifest["files"][1]["file"] = "candles-000002.parquet"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="missing"):
        AnalyticalCandleStore(target)


def test_filtered_quarter_exports_count_only_the_selected_source_slice(tmp_path):
    store, original = fixture_store(tmp_path)
    later = replace(original, open_time=OPEN + 100 * 86400, close_time=OPEN + 100 * 86400 + 3600,
                    observed_at=OPEN + 100 * 86400 + 3600)
    store.put_candles([later, replace(later, quote="USD"), replace(later, source="other")])
    start, end = OPEN + 90 * 86400, OPEN + 180 * 86400
    manifest = export_candle_snapshot(store, tmp_path / "quarter", assets=["BTC"], source="venue", quote="EUR",
                                      mode="live", start=start, end=end, max_rows=1, rows_per_file=1)
    assert manifest["rows"] == 1
    result = AnalyticalCandleStore(tmp_path / "quarter").candles(["BTC"], start=start, end=end, as_of=end,
                                                               source="venue", quote="EUR")
    assert result == [later]


def test_export_byte_deadline_and_partition_budgets_clean_staging(tmp_path, monkeypatch):
    store, _ = fixture_store(tmp_path)
    with pytest.raises(ValueError, match="byte budget"):
        export_candle_snapshot(store, tmp_path / "bytes", rows_per_file=2, max_bytes=1)
    assert not (tmp_path / "bytes").exists()
    assert not list(tmp_path.glob(".bytes-*"))
    monkeypatch.setattr(analytical_store, "MAX_PARTITIONS", 2)
    with pytest.raises(ValueError, match="partition-count"):
        export_candle_snapshot(store, tmp_path / "parts", rows_per_file=1)
    moments = iter([0., 2.])
    monkeypatch.setattr(analytical_store, "time", SimpleNamespace(monotonic=lambda: next(moments)))
    with pytest.raises(ValueError, match="deadline"):
        export_candle_snapshot(store, tmp_path / "timeout", max_seconds=1)
    assert not (tmp_path / "timeout").exists()
    assert not list(tmp_path.glob(".timeout-*"))


def test_read_query_deadline_interrupts_real_engine_without_spill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(analytical_store, "_QUERY_SECONDS", .02)
    with pytest.raises(ValueError, match="deadline"):
        with analytical_store._query_connection() as con:
            con.execute("SELECT SUM(a.i*b.i) FROM range(1000000) a(i),range(1000000) b(i)").fetchall()
    assert list(tmp_path.iterdir()) == []


def test_read_memory_budget_fails_without_creating_temp_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(analytical_store, "_QUERY_MEMORY", "1MB")
    with pytest.raises(ValueError, match="read-only SQL budget"):
        with analytical_store._query_connection() as con:
            assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == ""
            con.execute("SELECT i FROM range(1000000) t(i) ORDER BY hash(i)").fetchall()
    assert list(tmp_path.iterdir()) == []


def test_heavy_read_admission_fails_fast_and_recovers_without_an_unbounded_queue():
    with analytical_store._query_connection():
        with analytical_store._query_connection():
            with pytest.raises(ValueError, match="capacity is busy"):
                with analytical_store._query_connection():
                    pytest.fail("A third heavy reader must be refused.")
    with analytical_store._query_connection() as con:
        assert con.execute("SELECT 1").fetchone() == (1,)
