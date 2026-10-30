"""Real SQLite forecasts with synthetic fixtures and a controlled issuance clock."""

import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest

from crypto_market_analysis.cross_asset import prospective
from crypto_market_analysis.cross_asset.prospective import (
    evaluate_market_assessment,
    freeze_market_protocol,
    issue_market_assessment,
)
from crypto_market_analysis.cross_asset.storage import CrossAssetStore


def epoch(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


NOW = epoch("2026-03-06T21:01:00Z")
MONDAY_CLOSE = epoch("2026-03-09T20:00:00Z")


def fixtures(mode="live"):
    sessions = []
    for day, opening, close_time in [("2026-03-05", "14:30", "21:00"), ("2026-03-06", "14:30", "21:00"),
                                 ("2026-03-09", "13:30", "20:00"), ("2026-03-10", "13:30", "20:00")]:
        sessions.append({"date": day, "segments": [{"start": epoch(f"{day}T{opening}:00Z"),
                                                    "end": epoch(f"{day}T{close_time}:00Z")}]})
    calendar = {"schema": "market-calendar.v1", "id": "XNYS:fixture", "version": "test.v1",
                "timezone": "America/New_York", "source_url": "https://example.test/calendar",
                "known_at": epoch("2026-03-01T00:00:00Z"), "supported_start": epoch("2026-03-05T00:00:00Z"),
                "supported_end": epoch("2026-03-11T00:00:00Z"), "profile": "explicit", "sessions": sessions,
                "provider_delay_seconds": 0, "halts": []}
    descriptor = {"id": "abc:raw", "instrument_id": "equity:abc", "listing_id": "abc:xnys:usd",
                  "provider": "fixture", "provider_symbol": "ABC", "native_currency": "USD", "unit": "share",
                  "price_basis": "raw", "data_mode": mode, "calendar_id": calendar["id"],
                  "calendar_version": calendar["version"], "interval": "session", "revision_semantics": "point_in_time"}
    start = calendar["known_at"]
    registry = {"schema": "market-registry.v1", "instruments": [
        {"id": "equity:abc", "name": "Synthetic ABC", "asset_class": "equity", "form": "share"}],
        "listings": [{"id": descriptor["listing_id"], "instrument_id": descriptor["instrument_id"],
                      "native_currency": "USD", "venue_mic": "XNYS", "calendar_id": calendar["id"],
                      "active_from": start, "active_until": None, "known_at": start}],
        "aliases": [{"provider": "fixture", "symbol": "ABC", "instrument_id": descriptor["instrument_id"],
                     "listing_id": descriptor["listing_id"], "effective_from": start,
                     "effective_until": None, "known_at": start}]}
    anchor = {**sessions[1]["segments"][0], "open": 100.0, "high": 101.0, "low": 99.0,
              "close": 100.0, "observed_at": NOW, "session_date": "2026-03-06"}
    bundle = {"schema": "market-bundle.v1", "registry": registry, "calendars": [calendar],
              "series": [{"schema": "market-series.v1", "descriptor": descriptor, "bars": [anchor], "actions": []}]}
    declaration = {"schema": "market-protocol.v1", "series": descriptor, "calendar": calendar,
                   "method": "directional-price-only.v1", "horizon": {"kind": "sessions", "value": 1, "deadline_rule": "exact"},
                   "eligibility": {"start": NOW - 120, "end": NOW + 86400}, "action_handling": "price_only",
                   "max_receipt_lag_seconds": 120}
    return bundle, declaration


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(prospective.time, "time", lambda: NOW)
    bundle, declaration = fixtures()
    database = tmp_path / "observer.sqlite"
    CrossAssetStore(database, initialize=True).import_bundle(bundle, received_at=NOW)
    protocol = freeze_market_protocol(declaration, frozen_at=NOW - 180)
    request = {"schema": "market-assessment-request.v1", "protocol": protocol,
               "probability_up": 0.75, "idempotency_key": "test:1"}
    return database, bundle, request


def append_target(database, bundle, *, receipt=MONDAY_CLOSE + 60, close=110.0, split=False):
    update = copy.deepcopy(bundle)
    segment = update["calendars"][0]["sessions"][2]["segments"][0]
    update["series"][0]["bars"] = [{**segment, "open": close, "high": close, "low": close,
                                      "close": close, "observed_at": MONDAY_CLOSE + 60, "session_date": "2026-03-09"}]
    if split:
        update["series"][0]["actions"] = [{"id": "split:1", "kind": "split", "ratio": 2.0,
                                            "effective_at": segment["start"], "announced_at": NOW,
                                            "observed_at": MONDAY_CLOSE + 60}]
    CrossAssetStore(database, initialize=True).import_bundle(update, received_at=receipt)


def test_frozen_hash_is_deterministic_and_mutation_fails_before_writes(setup):
    database, _, request = setup
    protocol = request["protocol"]
    assert protocol == freeze_market_protocol(protocol["declaration"], frozen_at=protocol["frozen_at"])
    forged = copy.deepcopy(request)
    forged["protocol"]["declaration"]["series"]["native_currency"] = "EUR"
    with pytest.raises(ValueError, match="mismatch"):
        issue_market_assessment(database, forged, issued_at=NOW)
    with sqlite3.connect(database) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='cross_asset_assessments'").fetchone()


def test_weekend_and_dst_session_horizon_binds_monday_native_close_and_brier(setup):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    assert assessment["target"]["end"] == MONDAY_CLOSE
    assert assessment["target"]["start"] == epoch("2026-03-09T13:30:00Z")
    assert assessment["anchor"]["close"] == 100
    pending = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE - 1)
    assert pending["status"] == "pending"
    append_target(database, bundle)
    outcome = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 60)
    assert outcome["status"] == "scored"
    assert outcome["outcome_up"] == 1
    assert outcome["brier_score"] == pytest.approx(0.0625)
    assert outcome["price_return"] == pytest.approx(0.10)
    assert outcome["evidence_scope"] == "locally_witnessed_live"


def test_late_local_receipt_cannot_repair_earlier_outcome_and_is_excluded(setup):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    append_target(database, bundle, receipt=MONDAY_CLOSE + 300)
    query = {"assessment_id": assessment["assessment_id"]}
    assert evaluate_market_assessment(database, query, as_of=MONDAY_CLOSE + 100)["status"] == "missing"
    late = evaluate_market_assessment(database, query, as_of=MONDAY_CLOSE + 300)
    assert late["status"] == "excluded"
    assert "late" in late["reason"]
    assert late["brier_score"] is None


def test_split_crossing_raw_price_window_excludes_score(setup):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    append_target(database, bundle, close=55, split=True)
    result = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 60)
    assert result["status"] == "excluded"
    assert "split" in result["reason"]


@pytest.mark.parametrize("boundary", ["anchor_end", "target_end"])
def test_split_exactly_at_either_raw_price_boundary_excludes_score(setup, boundary):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    action_bundle = copy.deepcopy(bundle)
    effective_at = assessment["anchor"]["end"] if boundary == "anchor_end" else MONDAY_CLOSE
    observed_at = NOW if boundary == "anchor_end" else MONDAY_CLOSE + 60
    action_bundle["series"][0]["actions"] = [{"id": f"split:{boundary}", "kind": "split", "ratio": 2.0,
                                              "effective_at": effective_at, "announced_at": NOW,
                                              "observed_at": observed_at}]
    CrossAssetStore(database, initialize=True).import_bundle(action_bundle, received_at=observed_at)
    append_target(database, bundle, close=50)
    result = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 60)
    assert result["status"] == "excluded"
    assert result["brier_score"] is None
    assert "split" in result["reason"]


def test_issuance_is_idempotent_and_original_row_is_append_only(setup):
    database, _, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    assert issue_market_assessment(database, request, issued_at=NOW) == assessment
    conflicting = {**request, "probability_up": 0.25}
    with pytest.raises(ValueError, match="different request"):
        issue_market_assessment(database, conflicting, issued_at=NOW)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM cross_asset_assessments").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM cross_asset_assessments")


@pytest.mark.parametrize("field,value", [("price_basis", "total_return"), ("price_basis", "split_adjusted"),
                                         ("revision_semantics", "captured_history"), ("data_mode", "import")])
def test_unsupported_basis_and_self_declared_imports_are_not_prospective(field, value):
    _, declaration = fixtures()
    declaration["series"][field] = value
    with pytest.raises(ValueError):
        freeze_market_protocol(declaration, frozen_at=NOW - 180)


def test_calendar_day_horizon_preserves_venue_wall_clock_through_dst(setup):
    database, _, request = setup
    declaration = copy.deepcopy(request["protocol"]["declaration"])
    declaration["horizon"] = {"kind": "calendar_days", "value": 2, "deadline_rule": "next_session"}
    request["protocol"] = freeze_market_protocol(declaration, frozen_at=NOW - 180)
    assert issue_market_assessment(database, request, issued_at=NOW)["target"]["end"] == MONDAY_CLOSE


def test_error_paths_never_initialize_database_or_assessment_schema(tmp_path, setup):
    _, _, request = setup
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="missing"):
        issue_market_assessment(missing, request, issued_at=NOW)
    with pytest.raises(ValueError, match="missing"):
        evaluate_market_assessment(missing, {"assessment_id": "0" * 64}, as_of=NOW)
    assert not missing.exists()
    with pytest.raises(ValueError, match="actual current clock"):
        issue_market_assessment(missing, request, issued_at=NOW - 10)


def test_mismatched_stored_lineage_and_unknown_deadline_fail_without_writes(setup):
    database, _, request = setup
    declaration = copy.deepcopy(request["protocol"]["declaration"])
    declaration["series"]["unit"] = "different unit"
    request["protocol"] = freeze_market_protocol(declaration, frozen_at=NOW - 180)
    with pytest.raises(ValueError, match="lineage"):
        issue_market_assessment(database, request, issued_at=NOW)
    declaration["series"]["unit"] = "share"
    declaration["horizon"] = {"kind": "elapsed", "value": 3600, "deadline_rule": "exact"}
    request["protocol"] = freeze_market_protocol(declaration, frozen_at=NOW - 180)
    with pytest.raises(ValueError, match="native observation"):
        issue_market_assessment(database, request, issued_at=NOW)
    with sqlite3.connect(database) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='cross_asset_assessments'").fetchone()


def test_evaluation_is_read_only_and_probability_zero_scores_correctly(setup):
    database, bundle, request = setup
    request["probability_up"] = 0.0
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    append_target(database, bundle, close=90)
    with sqlite3.connect(database) as con:
        before = con.execute("SELECT payload FROM cross_asset_assessments").fetchone()[0]
    result = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 60)
    assert result["outcome_up"] == 0 and result["brier_score"] == 0
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT payload FROM cross_asset_assessments").fetchone()[0] == before
        assert json.loads(before)["issued_at"] == NOW


def test_later_correction_cannot_replace_the_preregistered_receipt_window(setup):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    append_target(database, bundle, close=110)
    correction = copy.deepcopy(bundle)
    segment = correction["calendars"][0]["sessions"][2]["segments"][0]
    correction["series"][0]["bars"] = [{**segment, "open": 90.0, "high": 90.0, "low": 90.0,
                                          "close": 90.0, "observed_at": MONDAY_CLOSE + 600,
                                          "session_date": "2026-03-09"}]
    CrossAssetStore(database, initialize=True).import_bundle(correction, received_at=MONDAY_CLOSE + 600)
    result = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 600)
    assert result["status"] == "scored" and result["outcome_up"] == 1
    assert result["target"]["observed_at"] == MONDAY_CLOSE + 60


def test_demo_forecasts_and_scores_retain_synthetic_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(prospective.time, "time", lambda: NOW)
    bundle, declaration = fixtures(mode="demo")
    database = tmp_path / "demo.sqlite"
    CrossAssetStore(database, initialize=True).import_bundle(bundle, received_at=NOW)
    request = {"schema": "market-assessment-request.v1", "idempotency_key": "demo:1", "probability_up": 0.5,
               "protocol": freeze_market_protocol(declaration, frozen_at=NOW - 180)}
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    append_target(database, bundle)
    result = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 60)
    assert result["status"] == "scored" and result["evidence_scope"] == "synthetic_demo"


@pytest.mark.parametrize("probability", [True, -0.1, 1.1, float("nan")])
def test_invalid_probabilities_never_create_an_assessment(setup, probability):
    database, _, request = setup
    request["probability_up"] = probability
    with pytest.raises(ValueError):
        issue_market_assessment(database, request, issued_at=NOW)
    with sqlite3.connect(database) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='cross_asset_assessments'").fetchone()


def test_eligibility_is_half_open_and_freeze_precedes_start(setup):
    database, _, request = setup
    declaration = copy.deepcopy(request["protocol"]["declaration"])
    declaration["eligibility"]["end"] = NOW
    request["protocol"] = freeze_market_protocol(declaration, frozen_at=NOW - 180)
    with pytest.raises(ValueError, match="eligibility"):
        issue_market_assessment(database, request, issued_at=NOW)
    with pytest.raises(ValueError, match="frozen before"):
        freeze_market_protocol(declaration, frozen_at=declaration["eligibility"]["start"])


def test_anchor_vintage_priority_uses_actual_receipt_before_declared_clock(setup):
    database, bundle, request = setup
    first = issue_market_assessment(database, request, issued_at=NOW)
    correction = copy.deepcopy(bundle)
    correction["series"][0]["bars"][0].update(observed_at=NOW - 30, open=120.0, high=120.0, low=120.0, close=120.0)
    CrossAssetStore(database, initialize=True).import_bundle(correction, received_at=NOW + 1)
    newer = issue_market_assessment(database, {**request, "idempotency_key": "newer-receipt"}, issued_at=NOW + 2)
    assert first["anchor"]["close"] == 100
    assert newer["anchor"]["close"] == 120
    assert newer["anchor"]["observed_at"] == NOW - 30
    assert newer["anchor_receipt"]["effective_known_at"] == NOW + 1


def test_target_vintage_priority_and_cutoffs_use_actual_receipt(setup):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    updates = copy.deepcopy(bundle)
    segment = updates["calendars"][0]["sessions"][2]["segments"][0]
    for declared_delay, actual_delay, close in ((90, 90, 110.0), (60, 100, 90.0)):
        updates["series"][0]["bars"] = [{**segment, "open": close, "high": close, "low": close,
                                         "close": close, "observed_at": MONDAY_CLOSE + declared_delay,
                                         "session_date": "2026-03-09"}]
        CrossAssetStore(database, initialize=True).import_bundle(updates, received_at=MONDAY_CLOSE + actual_delay)
    query = {"assessment_id": assessment["assessment_id"]}
    earlier = evaluate_market_assessment(database, query, as_of=MONDAY_CLOSE + 95)
    later = evaluate_market_assessment(database, query, as_of=MONDAY_CLOSE + 100)
    assert earlier["status"] == later["status"] == "scored"
    assert earlier["outcome_up"] == 1 and later["outcome_up"] == 0
    assert later["target"]["observed_at"] == MONDAY_CLOSE + 60
    assert later["target_receipt"]["effective_known_at"] == MONDAY_CLOSE + 100
    assert later["brier_score"] == pytest.approx(0.5625)


def test_issuance_uses_one_snapshot_despite_concurrent_backdated_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(prospective.time, "time", lambda: NOW)
    bundle, declaration = fixtures()
    bundle["series"][0]["bars"][0]["observed_at"] = NOW - 10
    database = tmp_path / "concurrent.sqlite"
    CrossAssetStore(database, initialize=True).import_bundle(bundle, received_at=NOW - 10)
    correction = copy.deepcopy(bundle)
    correction["series"][0]["bars"][0].update(observed_at=NOW - 20, open=120.0, high=120.0, low=120.0, close=120.0)
    original_reader = prospective._series

    def interleave_writer(reader, descriptor, cutoff):
        CrossAssetStore(database, initialize=True).import_bundle(correction, received_at=NOW)
        return original_reader(reader, descriptor, cutoff)

    monkeypatch.setattr(prospective, "_series", interleave_writer)
    request = {"schema": "market-assessment-request.v1", "probability_up": 0.75, "idempotency_key": "concurrent:1",
               "protocol": freeze_market_protocol(declaration, frozen_at=NOW - 180)}
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    assert assessment["anchor"]["close"] == 100
    latest = CrossAssetStore(database).read_series("abc:raw", as_of=NOW, data_mode="live")
    assert latest["bars"][0]["close"] == 120


def test_evaluation_reuses_snapshot_for_receipt_window_fallback(setup, monkeypatch):
    database, bundle, request = setup
    assessment = issue_market_assessment(database, request, issued_at=NOW)
    append_target(database, bundle, close=110)
    late = copy.deepcopy(bundle)
    segment = late["calendars"][0]["sessions"][2]["segments"][0]
    late["series"][0]["bars"] = [{**segment, "open": 80.0, "high": 80.0, "low": 80.0, "close": 80.0,
                                  "observed_at": MONDAY_CLOSE + 600, "session_date": "2026-03-09"}]
    CrossAssetStore(database, initialize=True).import_bundle(late, received_at=MONDAY_CLOSE + 600)
    correction = copy.deepcopy(late)
    correction["series"][0]["bars"][0].update(observed_at=MONDAY_CLOSE + 50, open=90.0, high=90.0, low=90.0, close=90.0)
    original_reader, inserted = prospective._series, False

    def interleave_writer(reader, descriptor, cutoff):
        nonlocal inserted
        result = original_reader(reader, descriptor, cutoff)
        if not inserted:
            inserted = True
            CrossAssetStore(database, initialize=True).import_bundle(correction, received_at=MONDAY_CLOSE + 100)
        return result

    monkeypatch.setattr(prospective, "_series", interleave_writer)
    result = evaluate_market_assessment(database, {"assessment_id": assessment["assessment_id"]}, as_of=MONDAY_CLOSE + 600)
    assert result["status"] == "scored" and result["outcome_up"] == 1
    assert result["target"]["close"] == 110
    latest = CrossAssetStore(database).read_series("abc:raw", as_of=MONDAY_CLOSE + 120, data_mode="live")
    assert latest["bars"][-1]["close"] == 90


def test_assessment_write_releases_read_snapshot_in_portable_delete_journal_mode(setup):
    database, _, request = setup
    portable = database.with_name("portable.sqlite")
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(portable)) as destination:
        source.backup(destination)
        assert destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    assessment = issue_market_assessment(portable, request, issued_at=NOW)
    assert assessment["anchor"]["close"] == 100
    with closing(sqlite3.connect(portable)) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("SELECT COUNT(*) FROM cross_asset_assessments").fetchone()[0] == 1
