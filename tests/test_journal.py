"""Immutable issuance exercises actual SQLite and market-retention boundaries."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from crypto_market_analysis.journal import AssessmentJournal
from crypto_market_analysis.models import Observation
from crypto_market_analysis.retention import compact_liquidity
from crypto_market_analysis.storage import Store


def test_retry_conflict_and_independent_return_values(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))
    original = journal.issue("key", {"expiry": 200, "config": {"quote": "EUR"}}, {"values": [1]}, 100)
    retry = journal.issue("key", {"config": {"quote": "EUR"}, "expiry": 200}, {"values": [1]}, 150)
    assert retry == original
    retry["evidence"]["values"].append(9)
    assert journal.get(original["assessment_id"]) == original
    with pytest.raises(ValueError, match="different assessment"):
        journal.issue("key", {"expiry": 201}, {}, 150)
    assert journal.list() == [original]
    assert original["provenance"]["code_content_hash"]
    assert original["provenance"]["dependency_lock_hash"]
    assert json.loads(json.dumps(original, allow_nan=False)) == original


def test_concurrent_retry_has_one_original(tmp_path):
    path = tmp_path / "market.sqlite"
    journal = AssessmentJournal(Store(path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda clock: journal.issue("same", {"status": "insufficient"}, {}, clock), range(8)))
    assert all(result == results[0] for result in results)
    assert len(journal.list()) == 1


def test_outcomes_append_and_never_change_original(tmp_path):
    store = Store(tmp_path / "market.sqlite")
    journal = AssessmentJournal(store)
    original = journal.issue("key", {"expiry": 120}, {}, 100)
    identity = original["assessment_id"]
    pending = journal.append_outcome(identity, "pending", {"status": "pending"}, 110)
    assert journal.append_outcome(identity, "pending", {"status": "pending"}, 500) == pending
    final = journal.append_outcome(identity, "resolved", {"status": "observed", "value": False}, 130)
    with pytest.raises(ValueError):
        journal.append_outcome(identity, "pending", {"status": "changed"}, 140)
    assert journal.outcomes(identity) == [pending, final]
    assert journal.get(identity) == original
    with store.connection() as con:
        with pytest.raises(sqlite3.IntegrityError, match="Immutable"):
            con.execute("UPDATE assessment_journal SET issued_at=900")
        with pytest.raises(sqlite3.IntegrityError, match="Append-only"):
            con.execute("DELETE FROM assessment_outcomes")


def test_replay_survives_revision_and_raw_retention(tmp_path):
    store = Store(tmp_path / "market.sqlite")
    observation = Observation("venue", "BTC", "depth_bid_1pct", 3600, 3600, 10, "EUR", "EUR")
    store.put_observations([observation])
    journal = AssessmentJournal(store)
    original = journal.issue("key", {"result": "elevated", "quote": "EUR"}, {"observations": [asdict(observation)]}, 3600)
    store.put_observations([replace(observation, value=1000, observed_at=7200)])
    compacted = compact_liquidity(store, keep_days=1, now=10 * 86400)
    assert compacted["raw_rows_deleted"] == 2
    reopened = AssessmentJournal(Store(store.path))
    assert reopened.get(original["assessment_id"]) == original
    assert reopened.get(original["assessment_id"])["evidence"]["observations"][0]["value"] == 10


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "bad"}, (1, 2), {"": 1}])
def test_invalid_json_does_not_create_original(tmp_path, bad):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))
    with pytest.raises(ValueError):
        journal.issue("key", {"bad": bad}, {}, 100)
    assert journal.list() == []


def test_bounded_inputs_and_missing_identity(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))
    for key in (True, "", "x" * 257):
        with pytest.raises(ValueError):
            journal.issue(key, {}, {})
    with pytest.raises(ValueError):
        journal.issue("big", {}, {"blob": "x" * (32 * 1024 * 1024)}, 100)
    with pytest.raises(ValueError):
        journal.issue("time", {}, {}, True)
    with pytest.raises(KeyError):
        journal.get("missing")
    with pytest.raises(KeyError):
        journal.append_outcome("missing", "outcome", {}, 100)
    assert journal.list() == []


def test_concurrent_conflicting_payload_is_atomic(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))

    def attempt(value):
        try:
            return journal.issue("same-key", {"value": value}, {}, 100)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert journal.list() == successful


@pytest.mark.parametrize("missing_database", [False, True])
def test_existing_reads_never_initialize_database_or_journal_schema(tmp_path, missing_database):
    store = Store(tmp_path / "market.sqlite")
    if missing_database:
        store.path.unlink()
    journal = AssessmentJournal.open_existing(store)
    assert journal.list() == []
    assert journal.original_for_request("retry", "request-hash") is None
    inputs = journal.summary_inputs(100)
    assert inputs.records == []
    assert inputs.outcomes == {}
    assert inputs.selection["available_records"] == 0
    for read in (journal.get, journal.outcomes):
        with pytest.raises(KeyError, match="no analyses have been issued"):
            read("missing")
    if missing_database:
        assert not store.path.exists()
    else:
        with sqlite3.connect(store.path) as con:
            names = {row[0] for row in con.execute("SELECT name FROM sqlite_master")}
        assert not any(name.startswith("assessment_") for name in names)


def test_summary_projects_large_evidence_without_changing_original_or_lineage(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))
    lineage = {"name": "frozen-research", "archive_hash": "original"}
    payload = {"request_hash": "request", "input_reference": {"available_at": 99,
               "dataset_lineage": lineage}}
    original = journal.issue("retry", payload, {"blob": "x" * (4 * 1024 * 1024)}, 100)
    inputs = journal.summary_inputs(100)
    assert inputs.records == [{key: value for key, value in original.items() if key != "evidence"}]
    assert len(json.dumps(inputs.records)) < 4096
    assert inputs.records[0]["payload"]["input_reference"]["dataset_lineage"] == lineage
    retry = journal.original_for_request("retry", "request")
    assert retry == original
    retry["payload"]["input_reference"]["dataset_lineage"]["name"] = "changed"
    assert journal.get(original["assessment_id"]) == original
    with pytest.raises(ValueError, match="different request"):
        journal.original_for_request("retry", "another-request")


def test_summary_cutoff_order_and_per_record_bounds_are_stable(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))
    older = journal.issue("old", {}, {}, 100)
    tied = [journal.issue(key, {}, {}, 200) for key in ("one", "two")]
    journal.issue("future", {}, {}, 600)
    expected = sorted(tied, key=lambda record: record["assessment_id"])
    identity = expected[0]["assessment_id"]
    journal.append_outcome(identity, "future-first", {}, 1000)
    appended = [journal.append_outcome(identity, str(index), {"index": index}, 499 - index)
                for index in range(257)]
    inputs = journal.summary_inputs(500, 2)
    assert [record["assessment_id"] for record in inputs.records] == [row["assessment_id"] for row in expected]
    assert older["assessment_id"] not in inputs.outcomes
    assert inputs.outcomes == {identity: appended[:256], expected[1]["assessment_id"]: []}
    assert inputs.selection == {"scope": "most_recent_issuances_at_or_before_cutoff", "limit": 2,
                                "available_records": 3, "records_truncated": True,
                                "outcome_limit_per_record": 256, "outcomes_truncated": [identity]}
    assert journal.summary_inputs(500, 2) == inputs
    assert len(journal.outcomes(identity)) == 258


@pytest.mark.parametrize("as_of,limit", [(True, 20), (-1, 20), (100, True), (100, 0), (100, 101)])
def test_summary_rejects_invalid_bounds_without_initializing_schema(tmp_path, as_of, limit):
    journal = AssessmentJournal.open_existing(Store(tmp_path / "market.sqlite"))
    with pytest.raises(ValueError, match="journal limit"):
        journal.summary_inputs(as_of, limit)


def test_summary_count_and_records_share_a_snapshot_during_issuance(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "market.sqlite"))

    def write_originals():
        for index in range(40):
            journal.issue(str(index), {}, {}, 100)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write_originals)
        for _ in range(40):
            inputs = journal.summary_inputs(100, 100)
            assert inputs.selection["available_records"] == len(inputs.records)
            assert not inputs.selection["records_truncated"]
        writer.result()
    assert journal.summary_inputs(100, 100).selection["available_records"] == 40
