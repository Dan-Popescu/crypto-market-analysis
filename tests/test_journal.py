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


def test_validation_projection_uses_indexed_resource_limited_snapshot(tmp_path):
    from test_prospective_validation import DAY, frozen, original

    protocol = frozen()
    record = original(protocol)
    journal = AssessmentJournal(Store(tmp_path / "journal.sqlite"))
    issued = journal.issue("validation", record["payload"], {"archived": "excluded from projection"}, DAY)
    result = AssessmentJournal.open_existing(Store(journal.store.path, initialize=False)).validation_records(
        protocol["declaration"]["cohorts"], starts_at=DAY, ends_at=10 * DAY, as_of=3 * DAY)
    assert result.records[0]["assessment_id"] == issued["assessment_id"]
    assert "evidence" not in result.records[0]
    assert result.selection["resource_limits"] == {
        "read_session_seconds": 15, "sqlite_vm_steps": 50_000_000,
        "returned_bytes": 64 * 1024 * 1024, "single_value_bytes": 2 * 1024 * 1024,
        "archive_input_bytes": 32 * 1024 * 1024}
    with journal.store.read_session() as reader, reader.connection() as con:
        original_plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT assessment_id FROM assessment_validation_issues "
            "WHERE source='test' AND asset='BTC' AND quote='EUR' AND data_mode='import' "
            "AND dataset_lineage IS json_extract(?,'$') "
            "AND issued_at>=? AND issued_at<? ORDER BY issued_at,assessment_id LIMIT 101",
            ('{"name":"synthetic-research"}', DAY, 10 * DAY)).fetchall()
        outcome_plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT record FROM assessment_validation_outcomes WHERE assessment_id=? "
            "AND observed_at<=? ORDER BY observed_at,content_hash "
            "LIMIT 257", (issued["assessment_id"], 3 * DAY)).fetchall()
    assert any("assessment_validation_market" in row[3] for row in original_plan)
    assert any("assessment_outcome_observed" in row[3] for row in outcome_plan)
    assert not any("TEMP B-TREE" in row[3] for row in (*original_plan, *outcome_plan))


def test_validation_read_does_not_add_indexes_to_legacy_schema(tmp_path):
    from test_prospective_validation import DAY, frozen, original

    protocol = frozen()
    record = original(protocol)
    store = Store(tmp_path / "legacy.sqlite")
    journal = AssessmentJournal(store)
    journal.issue("validation", record["payload"], {}, DAY)
    with store.connection() as con:
        con.execute("DROP INDEX assessment_validation_market")
        con.execute("DROP INDEX assessment_outcome_observed")
    readonly = AssessmentJournal.open_existing(Store(store.path, initialize=False))
    result = readonly.validation_records(protocol["declaration"]["cohorts"], starts_at=DAY, ends_at=10 * DAY,
                                         as_of=3 * DAY)
    assert len(result.records) == 1
    with store.connection() as con:
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "assessment_validation_market" not in indexes
    assert "assessment_outcome_observed" not in indexes
    AssessmentJournal(store)
    with store.connection() as con:
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"assessment_validation_market", "assessment_outcome_observed"} <= indexes


def test_validation_projection_retains_one_snapshot_during_concurrent_append(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from test_prospective_validation import DAY, frozen, original, outcome

    protocol = frozen()
    store = Store(tmp_path / "journal.sqlite")
    journal = AssessmentJournal(store)
    record = journal.issue("validation", original(protocol)["payload"], {}, DAY)
    session = journal._validation_read_connection
    appended = False

    @contextmanager
    def observed_session():
        nonlocal appended
        with session() as con:
            def interleave(sql):
                nonlocal appended
                if "SELECT record FROM assessment_validation_outcomes" in sql and not appended:
                    appended = True
                    journal.append_outcome(record["assessment_id"], "later", outcome(record)["payload"], 2 * DAY)
            con.set_trace_callback(interleave)
            yield con

    monkeypatch.setattr(journal, "_validation_read_connection", observed_session)
    cohorts = protocol["declaration"]["cohorts"]
    first = journal.validation_records(cohorts, starts_at=DAY, ends_at=10 * DAY, as_of=3 * DAY)
    assert appended is True
    assert first.outcomes[record["assessment_id"]] == []
    second = journal.validation_records(cohorts, starts_at=DAY, ends_at=10 * DAY, as_of=3 * DAY)
    assert len(second.outcomes[record["assessment_id"]]) == 1


def _drop_validation_projection_schema(store):
    with store.connection() as con:
        for table in ("assessment_validation_issues", "assessment_validation_outcomes", "assessment_validation_migration"):
            con.execute(f"DROP TABLE {table}")


def test_large_valid_archives_remain_readable_before_and_after_explicit_migration(tmp_path):
    from test_prospective_validation import DAY, frozen, original, outcome

    from crypto_market_analysis.prospective_validation import evaluate_stored_validation

    protocol = frozen()
    store = Store(tmp_path / "large.sqlite")
    journal = AssessmentJournal(store)
    record = journal.issue("large", original(protocol)["payload"], {"archive": "x" * (3 * 1024 * 1024)}, DAY)
    payload = outcome(record)["payload"]
    payload["unused_archived_evidence"] = "y" * (3 * 1024 * 1024)
    appended = journal.append_outcome(record["assessment_id"], "large", payload, 2 * DAY)
    persisted = evaluate_stored_validation(store, protocol, as_of=3 * DAY)
    assert persisted["cohorts"][0]["events"][0]["counts"]["resolved"] == 1
    assert persisted["selection"]["projection_mode"] == "persisted"
    _drop_validation_projection_schema(store)
    before = store.path.read_bytes()
    legacy = evaluate_stored_validation(Store(store.path, initialize=False), protocol, as_of=3 * DAY)
    assert legacy["selection"]["projection_mode"] == "bounded_legacy_projection"
    assert legacy["cohorts"] == persisted["cohorts"]
    assert store.path.read_bytes() == before
    AssessmentJournal(store)
    migrated = evaluate_stored_validation(store, protocol, as_of=3 * DAY)
    assert migrated["selection"]["projection_mode"] == "persisted"
    assert migrated["cohorts"] == persisted["cohorts"]
    assert journal.get(record["assessment_id"]) == record
    assert journal.outcomes(record["assessment_id"]) == [appended]


def test_projection_backfill_advances_in_explicit_bounded_batches(tmp_path):
    from test_prospective_validation import DAY, frozen, original

    store = Store(tmp_path / "migration.sqlite")
    journal = AssessmentJournal(store)
    protocol = frozen()
    for index in range(105):
        journal.issue(str(index), original(protocol, str(index))["payload"], {}, DAY)
    _drop_validation_projection_schema(store)
    migrated = AssessmentJournal(store)
    with store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM assessment_validation_issues").fetchone()[0] == 100
    partial = migrated.validation_records(protocol["declaration"]["cohorts"], starts_at=DAY, ends_at=10 * DAY,
                                          as_of=3 * DAY)
    assert partial.selection["projection_mode"] == "bounded_legacy_projection"
    result = migrated.migrate_validation_projections(record_limit=3)
    assert result["migrated_issues"] == 3
    assert result["complete"] is False
    result = migrated.migrate_validation_projections(record_limit=3)
    assert result["migrated_issues"] == 2
    assert result["complete"] is True
    ready = migrated.validation_records(protocol["declaration"]["cohorts"], starts_at=DAY, ends_at=10 * DAY,
                                        as_of=3 * DAY)
    assert ready.selection["projection_mode"] == "persisted"
    with pytest.raises(ValueError, match="writable journal"):
        AssessmentJournal.open_existing(Store(store.path, initialize=False)).migrate_validation_projections()


def test_validation_snapshot_enforces_actual_work_and_projected_byte_budgets(tmp_path):
    journal = AssessmentJournal(Store(tmp_path / "budget.sqlite"))
    with pytest.raises(ValueError, match="50-million SQLite VM-step budget"):
        with journal._validation_read_connection() as con:
            con.execute("""WITH RECURSIVE rows(n) AS (VALUES(0) UNION ALL
                SELECT n+1 FROM rows WHERE n<100000000) SELECT SUM(n) FROM rows""").fetchone()
    with pytest.raises(ValueError, match="2 MiB returned-value"):
        with journal._validation_read_connection() as con:
            con.execute("SELECT zeroblob(2097153)").fetchone()
    with pytest.raises(ValueError, match="64 MiB returned-data"):
        with journal._validation_read_connection() as con:
            con.execute("""WITH RECURSIVE rows(n) AS (VALUES(0) UNION ALL
                SELECT n+1 FROM rows WHERE n<65) SELECT zeroblob(1048576) FROM rows""").fetchall()
    # Refused reads never poison or mutate the original writer.
    assert journal.issue("after-refusal", {}, {}, 100)["issued_at"] == 100


def test_projection_and_immutable_append_roll_back_as_one_write(tmp_path, monkeypatch):
    journal = AssessmentJournal(Store(tmp_path / "atomic.sqlite"))
    insert = journal._insert_validation_projection

    def refused(con, kind, rowid):
        insert(con, kind, rowid)
        raise ValueError("Simulated projection failure")

    monkeypatch.setattr(journal, "_insert_validation_projection", refused)
    with pytest.raises(ValueError, match="projection failure"):
        journal.issue("atomic", {"kind": "accumulation_assessment"}, {}, 100)
    assert journal.list() == []
    monkeypatch.setattr(journal, "_insert_validation_projection", insert)
    issued = journal.issue("atomic", {"kind": "accumulation_assessment"}, {}, 100)
    monkeypatch.setattr(journal, "_insert_validation_projection", refused)
    with pytest.raises(ValueError, match="projection failure"):
        journal.append_outcome(issued["assessment_id"], "atomic", {}, 200)
    assert journal.outcomes(issued["assessment_id"]) == []
    with journal.store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM assessment_validation_issues").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM assessment_validation_outcomes").fetchone()[0] == 0
