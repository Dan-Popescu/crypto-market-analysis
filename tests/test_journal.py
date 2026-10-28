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
