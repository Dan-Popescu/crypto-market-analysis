"""Recovery verifies native originals without installing legacy schemas."""

import copy
import json
import sqlite3

import pytest
from test_cross_asset_prospective import NOW, fixtures

from crypto_market_analysis.cross_asset import prospective
from crypto_market_analysis.cross_asset.prospective import freeze_market_protocol, issue_market_assessment
from crypto_market_analysis.cross_asset.recovery import verify_market_database
from crypto_market_analysis.cross_asset.storage import CrossAssetStore
from crypto_market_analysis.evidence import canonical_json, content_hash


def bundle(mode="import"):
    return {
        "schema": "market-bundle.v1", "registry": {"schema": "market-registry.v1", "instruments": [
            {"id": "commodity:reference", "name": "Synthetic negative reference", "asset_class": "commodity", "form": "reference"}],
            "listings": [], "aliases": [{"provider": "fixture", "symbol": "REF", "instrument_id": "commodity:reference",
                                        "listing_id": None, "effective_from": 0, "effective_until": None, "known_at": 0}]},
        "calendars": [{"schema": "market-calendar.v1", "id": "fixture", "version": "v1", "timezone": "UTC",
                       "source_url": "https://example.test/calendar", "known_at": 0, "supported_start": 0,
                       "supported_end": 1000000, "profile": "explicit", "sessions": [
                           {"date": "1970-01-01", "segments": [{"start": 100, "end": 130}], "publication_at": 140}]}],
        "series": [{"schema": "market-series.v1", "descriptor": {"id": "reference:raw", "instrument_id": "commodity:reference",
                    "listing_id": None, "provider": "fixture", "provider_symbol": "REF", "native_currency": "USD",
                    "unit": "USD/barrel", "price_basis": "raw", "data_mode": mode, "calendar_id": "fixture",
                    "calendar_version": "v1", "interval": "reference", "revision_semantics": "captured_history"},
                    "bars": [{"start": 100, "end": 130, "open": -20, "high": -10, "low": -30, "close": -15, "observed_at": 140}],
                    "actions": []}],
    }


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "observer.sqlite"
    CrossAssetStore(path, initialize=True).import_bundle(bundle(), received_at=200)
    return path


def verify(path):
    with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True) as con:
        return verify_market_database(con)


def tamper(path, table, statement, args=()):
    """Preserve guards so integrity tests reach the changed data contract."""
    with sqlite3.connect(path) as con:
        triggers = con.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
        for name, _ in triggers:
            con.execute(f'DROP TRIGGER "{name}"')
        con.execute(statement, args)
        for _, sql in triggers:
            con.execute(sql)


def test_native_only_database_verifies_exact_negative_originals_without_mutation(database):
    before = CrossAssetStore(database).read_series("reference:raw", as_of=150, data_mode="import")
    with sqlite3.connect(database) as con:
        statements = []
        con.set_trace_callback(statements.append)
        checked = verify_market_database(con)
        assert con.total_changes == 0
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    assert checked["tables_verified"] == 9
    assert checked["rows_verified"]["bars"] == 1
    assert checked["assessments_verified"] == 0
    assert checked["bundle_digest_scope"] == "lineage_reference_without_original_bundle_bytes"
    assert not any(statement.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER")) for statement in statements)
    after = CrossAssetStore(database).read_series("reference:raw", as_of=150, data_mode="import")
    assert before == after and after["bars"][0]["close"] == -15
    assert after["storage_provenance"]["bars"][0]["hash"] == content_hash(bundle()["series"][0]["bars"][0])


@pytest.mark.parametrize("statement,match", [
    ("DROP TABLE cross_asset_actions", "table contract"),
    ("DROP INDEX cross_asset_bar_cutoff", "query index"),
    ("DROP TRIGGER cross_asset_bars_no_update", "append-only guard"),
])
def test_missing_required_native_schema_is_rejected(database, statement, match):
    with sqlite3.connect(database) as con:
        con.execute(statement)
    with pytest.raises(ValueError, match=match):
        verify(database)


def test_same_name_noop_trigger_does_not_count_as_immutability_guard(database):
    with sqlite3.connect(database) as con:
        con.execute("DROP TRIGGER cross_asset_bars_no_update")
        con.execute("CREATE TRIGGER cross_asset_bars_no_update BEFORE UPDATE ON cross_asset_bars BEGIN SELECT 1; END")
    with pytest.raises(ValueError, match="append-only guard"):
        verify(database)


@pytest.mark.parametrize("table,statement,match", [
    ("cross_asset_bars", "UPDATE cross_asset_bars SET start=101", "indexed bounds"),
    ("cross_asset_bars", "UPDATE cross_asset_bars SET known_at=141", "visibility mismatch"),
    ("cross_asset_bars", "UPDATE cross_asset_bars SET import_hash='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'", "orphan"),
    ("cross_asset_series", "UPDATE cross_asset_series SET native_currency='EUR'", "indexed native_currency"),
    ("cross_asset_calendars", "UPDATE cross_asset_calendars SET version='v2'", "indexed dimensions"),
    ("cross_asset_admissions", "DELETE FROM cross_asset_admissions WHERE kind='calendar'", "admission is missing"),
    ("cross_asset_registry_components", "DELETE FROM cross_asset_registry_components WHERE kind='instruments'", "missing registry originals"),
])
def test_native_indices_clocks_and_cross_references_are_verified(database, table, statement, match):
    tamper(database, table, statement)
    with pytest.raises(ValueError, match=match):
        verify(database)


def test_native_payload_hash_is_checked_without_reinterpreting_prices(database):
    changed = copy.deepcopy(bundle()["series"][0]["bars"][0])
    changed["close"] = -14
    tamper(database, "cross_asset_bars", "UPDATE cross_asset_bars SET payload=?", (canonical_json(changed),))
    with pytest.raises(ValueError, match="payload hash mismatch"):
        verify(database)


def test_live_receipt_visibility_and_historical_backfill_admissions_verify(tmp_path):
    path = tmp_path / "observer.sqlite"
    store = CrossAssetStore(path, initialize=True)
    store.import_bundle(bundle("live"), received_at=500)
    historical = bundle("import")
    historical["series"][0]["descriptor"]["id"] = "reference:import"
    store.import_bundle(historical, received_at=600)
    assert verify(path)["rows_verified"]["admissions"] == 6
    assert store.calendar("fixture", "v1", as_of=140)
    tamper(path, "cross_asset_bars", "UPDATE cross_asset_bars SET known_at=140 WHERE series_id='reference:raw'")
    with pytest.raises(ValueError, match="visibility mismatch"):
        verify(path)


def test_original_assessment_hash_and_frozen_protocol_are_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(prospective.time, "time", lambda: NOW)
    original, declaration = fixtures()
    path = tmp_path / "observer.sqlite"
    CrossAssetStore(path, initialize=True).import_bundle(original, received_at=NOW)
    request = {"schema": "market-assessment-request.v1", "probability_up": 0.75, "idempotency_key": "verify:1",
               "protocol": freeze_market_protocol(declaration, frozen_at=NOW - 180)}
    assessment = issue_market_assessment(path, request, issued_at=NOW)
    assert verify(path)["assessments_verified"] == 1
    assessment["protocol"]["protocol_sha256"] = "f" * 64
    assessment["assessment_id"] = content_hash({key: value for key, value in assessment.items() if key != "assessment_id"})
    tamper(path, "cross_asset_assessments", "UPDATE cross_asset_assessments SET id=?,payload=?",
           (assessment["assessment_id"], canonical_json(assessment)))
    with pytest.raises(ValueError, match="frozen protocol mismatch"):
        verify(path)


def test_unknown_empty_database_is_not_initialized(tmp_path):
    path = tmp_path / "empty.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE user_table (data TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="table contract"):
        verify(path)
    assert path.read_bytes() == before


def test_noncanonical_original_json_is_rejected_even_with_unchanged_semantic_hash(database):
    bar = bundle()["series"][0]["bars"][0]
    tamper(database, "cross_asset_bars", "UPDATE cross_asset_bars SET payload=?", (json.dumps(bar, indent=2),))
    with pytest.raises(ValueError, match="noncanonical original"):
        verify(database)
