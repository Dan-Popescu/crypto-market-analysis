"""Offline native/vintage journal and immutable analytical export checks."""

import json
import sqlite3
from copy import deepcopy

import pytest

from crypto_market_analysis.cross_asset.snapshots import MarketSnapshot, export_market_snapshot
from crypto_market_analysis.cross_asset.storage import CrossAssetStore
from crypto_market_analysis.evidence import content_hash
from crypto_market_analysis.recovery import create_backup, verify_backup
from crypto_market_analysis.storage import Store


def bundle(mode="import", sid="reference-raw"):
    return {
        "schema": "market-bundle.v1",
        "registry": {"schema": "market-registry.v1", "instruments": [
            {"id": "reference", "name": "Synthetic commodity reference", "asset_class": "commodity", "form": "reference"}],
            "listings": [], "aliases": [{"provider": "synthetic", "symbol": "REF", "instrument_id": "reference",
                                        "listing_id": None, "effective_from": 0, "effective_until": None, "known_at": 0}]},
        "calendars": [{"schema": "market-calendar.v1", "id": "synthetic-calendar", "version": "v1",
                       "timezone": "UTC", "source_url": "https://example.invalid/synthetic", "known_at": 0,
                       "supported_start": 0, "supported_end": 1_000_000, "profile": "explicit",
                       "sessions": [{"date": "1970-01-01", "segments": [{"start": 100, "end": 130}], "publication_at": 140}]}],
        "series": [{"schema": "market-series.v1", "descriptor": {
            "id": sid, "instrument_id": "reference", "listing_id": None, "provider": "synthetic", "provider_symbol": "REF",
            "native_currency": "USD", "unit": "USD/barrel", "price_basis": "raw", "data_mode": mode,
            "calendar_id": "synthetic-calendar", "calendar_version": "v1", "interval": "reference",
            "revision_semantics": "captured_history"}, "bars": [
                {"start": 100, "end": 130, "open": -20, "high": -10, "low": -30, "close": -15, "observed_at": 140}],
            "actions": []}],
    }


def writer(tmp_path):
    return CrossAssetStore(tmp_path / "observer.sqlite", initialize=True)


def test_readers_do_not_create_or_migrate_existing_authority(tmp_path):
    path = tmp_path / "missing" / "observer.sqlite"
    reader = CrossAssetStore(path)
    assert reader.registry(1000) is None
    assert reader.calendar("absent", as_of=1000) is None
    assert reader.list_series(as_of=1000, data_mode="live")["series"] == []
    assert reader.read_series("absent", as_of=1000, data_mode="live") is None
    assert not path.parent.exists()
    with pytest.raises(ValueError, match="explicit"):
        reader.import_bundle(bundle(), received_at=200)
    path.parent.mkdir()
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE legacy (payload TEXT)")
    before = path.read_bytes()
    assert reader.registry(1000) is None
    assert path.read_bytes() == before
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("legacy",)]


def test_native_negative_prices_exact_hashes_vintages_and_atomic_conflicts(tmp_path):
    store = writer(tmp_path)
    original = bundle()
    store.import_bundle(original, received_at=200)
    replay = store.import_bundle(original, received_at=300)
    assert replay["idempotent"] is True and replay["received_at"] == 200
    first = store.read_series("reference-raw", as_of=150, data_mode="import")
    assert first["bars"] == original["series"][0]["bars"]
    assert first["storage_provenance"]["bars"][0] == {
        "hash": content_hash(original["series"][0]["bars"][0]), "declared_observed_at": 140,
        "received_at": 200, "effective_known_at": 140}
    assert first["storage_provenance"]["receipt_evidence"] == "self_declared_history"
    revised = deepcopy(original)
    revised["series"][0]["bars"][0].update(observed_at=240, close=-12)
    store.import_bundle(revised, received_at=300)
    assert store.read_series("reference-raw", as_of=239, data_mode="import")["bars"][0]["close"] == -15
    assert store.read_series("reference-raw", as_of=240, data_mode="import")["bars"][0]["close"] == -12
    conflict = deepcopy(revised)
    conflict["series"][0]["bars"].insert(0, {"start": 100, "end": 130, "open": 1, "high": 2, "low": 1, "close": 2, "observed_at": 320})
    conflict["series"][0]["bars"][1]["close"] = -11
    with pytest.raises(ValueError, match="Conflicting"):
        store.import_bundle(conflict, received_at=400)
    assert store.read_series("reference-raw", as_of=500, data_mode="import")["bars"][0]["close"] == -12
    with sqlite3.connect(store.path) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE cross_asset_bars SET start=0")


def test_live_receipt_cannot_be_backdated_and_modes_bases_do_not_mix(tmp_path):
    store = writer(tmp_path)
    live = bundle("live", "live")
    store.import_bundle(live, received_at=500)
    assert store.read_series("live", as_of=499, data_mode="live") is None
    row = store.read_series("live", as_of=500, data_mode="live")
    assert row["bars"][0]["observed_at"] == 140
    assert row["storage_provenance"]["bars"][0]["effective_known_at"] == 500
    assert row["storage_provenance"]["receipt_evidence"] == "locally_witnessed"
    assert store.read_series("live", as_of=500, data_mode="demo") is None
    changed_basis = deepcopy(live)
    changed_basis["series"][0]["descriptor"]["price_basis"] = "total_return"
    with pytest.raises(ValueError, match="adjusted bases"):
        store.import_bundle(changed_basis, received_at=600)
    changed_unit = deepcopy(live)
    changed_unit["series"][0]["descriptor"]["unit"] = "USD/tonne"
    with pytest.raises(ValueError, match="immutable"):
        store.import_bundle(changed_unit, received_at=600)
    unknown_calendar = bundle(sid="other")
    unknown_calendar["series"][0]["descriptor"]["calendar_version"] = "unknown"
    with pytest.raises(ValueError, match="calendar version"):
        store.import_bundle(unknown_calendar, received_at=600)


def test_registry_identity_revisions_cannot_change_instrument_class(tmp_path):
    store = writer(tmp_path)
    original = bundle()
    store.import_bundle(original, received_at=200)
    changed = deepcopy(original)
    changed["registry"]["instruments"][0]["asset_class"] = "other"
    with pytest.raises(ValueError, match="immutable registry_components"):
        store.import_bundle(changed, received_at=300)
    assert store.registry(300) == original["registry"]


def test_bounded_reads_and_descriptor_pagination(tmp_path):
    store = writer(tmp_path)
    for sid in ("a", "b", "c"):
        store.import_bundle(bundle(sid=sid), received_at=200)
    page = store.list_series(as_of=500, data_mode="import", limit=2)
    assert [row["id"] for row in page["series"]] == ["a", "b"]
    assert page["next_cursor"] == "b"
    assert store.list_series(as_of=500, data_mode="import", limit=2, cursor="b")["series"][0]["id"] == "c"
    assert store.read_series("a", as_of=500, data_mode="import", start=130)["bars"] == []
    with pytest.raises(ValueError, match="max_rows"):
        store.read_series("a", as_of=500, data_mode="import", max_rows=100001)
    with pytest.raises(ValueError, match="limit"):
        store.list_series(as_of=500, data_mode="import", limit=1001)


def test_snapshot_preserves_native_all_vintages_and_is_frozen(tmp_path):
    store = writer(tmp_path)
    first = bundle()
    store.import_bundle(first, received_at=200)
    second = deepcopy(first)
    second["series"][0]["bars"][0].update(observed_at=240, close=-12)
    store.import_bundle(second, received_at=300)
    directory = tmp_path / "snapshot"
    manifest = export_market_snapshot(store, directory, batch_rows=1)
    assert manifest["format"] == "market-v1" and manifest["all_vintages"]
    snapshot = MarketSnapshot(directory)
    for cutoff in (150, 239, 240, 500):
        assert snapshot.read_series("reference-raw", as_of=cutoff, data_mode="import") == store.read_series("reference-raw", as_of=cutoff, data_mode="import")
    assert snapshot.registry(500) == store.registry(500)
    assert snapshot.calendar("synthetic-calendar", "v1", as_of=500) == store.calendar("synthetic-calendar", "v1", as_of=500)
    assert snapshot.list_series(as_of=500, data_mode="import") == store.list_series(as_of=500, data_mode="import")
    third = deepcopy(second)
    third["series"][0]["bars"][0].update(observed_at=400, close=-11)
    store.import_bundle(third, received_at=500)
    assert snapshot.read_series("reference-raw", as_of=500, data_mode="import")["bars"][0]["close"] == -12
    assert store.read_series("reference-raw", as_of=500, data_mode="import")["bars"][0]["close"] == -11
    with pytest.raises(ValueError, match="new directory"):
        export_market_snapshot(store, directory)


def test_snapshot_integrity_and_export_budgets_fail_without_partial_artifacts(tmp_path):
    store = writer(tmp_path)
    store.import_bundle(bundle(), received_at=200)
    with pytest.raises(ValueError, match="row budget"):
        export_market_snapshot(store, tmp_path / "bounded", max_rows=1)
    assert not (tmp_path / "bounded").exists()
    assert not list(tmp_path.glob(".bounded-*"))
    directory = tmp_path / "snapshot"
    manifest = export_market_snapshot(store, directory)
    snapshot = MarketSnapshot(directory)
    part = directory / next(item["file"] for item in manifest["files"] if item["table"] == "bars")
    original = part.read_bytes()
    part.write_bytes(original[:-1])
    with pytest.raises(ValueError, match="changed"):
        snapshot.read_series("reference-raw", as_of=500, data_mode="import")
    with pytest.raises(ValueError, match="checksum"):
        MarketSnapshot(directory)
    part.write_bytes(original)
    manifest["files"][0]["file"] = "../escape.parquet"
    manifest["content_sha256"] = content_hash({key: value for key, value in manifest.items() if key != "content_sha256"})
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        MarketSnapshot(directory)


def test_subregistry_union_later_known_aliases_and_snapshot_parity(tmp_path):
    store = writer(tmp_path)
    first = bundle()
    first["registry"]["aliases"].append({**first["registry"]["aliases"][0], "symbol": "LATER", "known_at": 300})
    store.import_bundle(first, received_at=400)
    assert [row["symbol"] for row in store.registry(200)["aliases"]] == ["REF"]
    second = bundle(sid="second")
    second["registry"]["instruments"][0].update(id="second", name="Second independent reference")
    second["registry"]["aliases"][0].update(instrument_id="second", symbol="SECOND")
    second["series"][0]["descriptor"].update(instrument_id="second", provider_symbol="SECOND")
    store.import_bundle(second, received_at=500)
    registry = store.registry(500)
    assert {row["id"] for row in registry["instruments"]} == {"reference", "second"}
    assert {row["symbol"] for row in registry["aliases"]} == {"REF", "LATER", "SECOND"}
    export_market_snapshot(store, tmp_path / "union")
    snapshot = MarketSnapshot(tmp_path / "union")
    assert snapshot.registry(200) == store.registry(200)
    assert snapshot.registry(500) == registry


def test_historical_backfill_adds_earlier_admission_without_rewriting_descriptor(tmp_path):
    store = writer(tmp_path)
    later = bundle()
    later["series"][0]["bars"][0]["observed_at"] = 240
    store.import_bundle(later, received_at=300)
    assert store.read_series("reference-raw", as_of=200, data_mode="import") is None
    earlier = bundle()
    store.import_bundle(earlier, received_at=400)
    assert store.read_series("reference-raw", as_of=200, data_mode="import")["bars"][0]["observed_at"] == 140
    export_market_snapshot(store, tmp_path / "backfill")
    assert MarketSnapshot(tmp_path / "backfill").read_series("reference-raw", as_of=200, data_mode="import") == store.read_series("reference-raw", as_of=200, data_mode="import")


def test_cumulative_fleet_limit_rolls_back_independent_registry(tmp_path):
    store = writer(tmp_path)
    fleet = {"schema": "market-bundle.v1", "registry": {"schema": "market-registry.v1", "instruments": [
        {"id": f"instrument-{index}", "name": f"Synthetic {index}", "asset_class": "commodity", "form": "reference"}
        for index in range(1000)], "listings": [], "aliases": []}, "calendars": [], "series": []}
    store.import_bundle(fleet, received_at=200)
    with pytest.raises(ValueError, match="Cumulative registry"):
        store.import_bundle(bundle(), received_at=300)
    assert len(store.registry(500)["instruments"]) == 1000
    assert store.read_series("reference-raw", as_of=500, data_mode="import") is None


def test_actions_share_combined_row_budget_and_are_cutoff_aware(tmp_path):
    store = writer(tmp_path)
    document = bundle()
    document["registry"]["instruments"][0]["form"] = "fund"
    document["series"][0]["descriptor"]["interval"] = "session"
    document["series"][0]["actions"] = [{"id": "split", "kind": "split", "effective_at": 150,
                                          "announced_at": 150, "observed_at": 200, "ratio": 2}]
    store.import_bundle(document, received_at=300)
    assert store.read_series("reference-raw", as_of=199, data_mode="import")["actions"] == []
    with pytest.raises(ValueError, match="row budget"):
        store.read_series("reference-raw", as_of=200, data_mode="import", max_rows=1)
    export_market_snapshot(store, tmp_path / "actions")
    snapshot = MarketSnapshot(tmp_path / "actions")
    assert snapshot.read_series("reference-raw", as_of=200, data_mode="import") == store.read_series("reference-raw", as_of=200, data_mode="import")
    with pytest.raises(ValueError, match="row budget"):
        snapshot.read_series("reference-raw", as_of=200, data_mode="import", max_rows=1)


def test_fleet_summary_preserves_native_dimensions_and_negative_admissibility(tmp_path):
    store = writer(tmp_path)
    negative = bundle(sid="negative")
    store.import_bundle(negative, received_at=200)
    positive = bundle(sid="positive")
    positive["series"][0]["descriptor"]["native_currency"] = "EUR"
    positive["series"][0]["descriptor"]["unit"] = "EUR/barrel"
    positive["series"][0]["bars"][0].update(open=20, high=30, low=10, close=25)
    store.import_bundle(positive, received_at=200)
    export_market_snapshot(store, tmp_path / "fleet")
    snapshot = MarketSnapshot(tmp_path / "fleet")
    result = snapshot.fleet_summary(["positive", "negative", "absent"], as_of=200, data_mode="import", start=100, end=130)
    assert result["total_rows"] == 2 and result["missing_series_ids"] == ["absent"]
    by_id = {row["series_id"]: row for row in result["series"]}
    assert by_id["negative"]["descriptor"]["native_currency"] == "USD"
    assert by_id["negative"]["first_close"] == -15
    assert by_id["negative"]["simple_return"] is None
    assert by_id["negative"]["simple_return_reason"] == "nonpositive_native_prices"
    assert by_id["positive"]["descriptor"]["native_currency"] == "EUR"
    assert by_id["positive"]["simple_return"] == 0
    with pytest.raises(ValueError, match="row budget"):
        snapshot.fleet_summary(["positive", "negative"], as_of=200, data_mode="import", start=100, end=130, max_rows=1)


def test_authority_read_transaction_is_consistent_across_concurrent_wal_commit(tmp_path):
    store = writer(tmp_path)
    original = bundle()
    store.import_bundle(original, received_at=200)
    with store.read_transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cross_asset_bars").fetchone()[0] == 1
        revised = deepcopy(original)
        revised["series"][0]["bars"][0].update(close=-12, observed_at=240)
        store.import_bundle(revised, received_at=300)
        assert connection.execute("SELECT COUNT(*) FROM cross_asset_bars").fetchone()[0] == 1
    assert store.read_series("reference-raw", as_of=500, data_mode="import")["bars"][0]["close"] == -12


def test_existing_online_backup_preserves_cross_asset_tables_and_original_hashes(tmp_path):
    legacy = Store(tmp_path / "observer.sqlite")
    store = CrossAssetStore(legacy.path, initialize=True)
    store.import_bundle(bundle(), received_at=200)
    target = tmp_path / "backup"
    create_backup(store.path, target)
    verify_backup(target)
    restored = CrossAssetStore(target / "observer.sqlite")
    assert restored.read_series("reference-raw", as_of=500, data_mode="import") == store.read_series("reference-raw", as_of=500, data_mode="import")


def test_unclosed_source_bars_are_rejected_before_journal_or_snapshot(tmp_path):
    store = writer(tmp_path)
    partial = bundle()
    partial["series"][0]["bars"][0]["observed_at"] = 120
    with pytest.raises(ValueError, match="observed before its end"):
        store.import_bundle(partial, received_at=200)
    assert store.read_series("reference-raw", as_of=500, data_mode="import") is None


def test_live_first_then_historical_import_preserves_independent_admission_clocks(tmp_path):
    store = writer(tmp_path)
    store.import_bundle(bundle("live", "live"), received_at=500)
    assert store.registry(200) is None
    assert store.calendar("synthetic-calendar", "v1", as_of=200) is None
    historical = bundle("import", "historical")
    store.import_bundle(historical, received_at=600)
    assert store.registry(200)["instruments"][0]["id"] == "reference"
    assert store.calendar("synthetic-calendar", "v1", as_of=200)["id"] == "synthetic-calendar"
    assert store.read_series("historical", as_of=200, data_mode="import")["bars"][0]["observed_at"] == 140
    assert store.read_series("live", as_of=200, data_mode="live") is None
    assert store.read_series("live", as_of=500, data_mode="live")["storage_provenance"]["bars"][0]["effective_known_at"] == 500
    export_market_snapshot(store, tmp_path / "mixed-admissions")
    snapshot = MarketSnapshot(tmp_path / "mixed-admissions")
    assert snapshot.registry(200) == store.registry(200)
    assert snapshot.calendar("synthetic-calendar", "v1", as_of=200) == store.calendar("synthetic-calendar", "v1", as_of=200)
    assert snapshot.read_series("historical", as_of=200, data_mode="import") == store.read_series("historical", as_of=200, data_mode="import")


def test_mixed_bundle_does_not_backdate_live_or_hide_declared_demo_history(tmp_path):
    store = writer(tmp_path)
    mixed = bundle("live", "live")
    mixed["series"].append(bundle("demo", "demo")["series"][0])
    store.import_bundle(mixed, received_at=500)
    assert store.read_series("live", as_of=200, data_mode="live") is None
    assert store.read_series("demo", as_of=200, data_mode="demo")["storage_provenance"]["receipt_evidence"] == "self_declared_history"
