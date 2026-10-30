"""Recovery probes use only synthetic records and real temporary SQLite files."""

import json
import sqlite3
import threading
from copy import deepcopy
from dataclasses import replace

import pytest

from crypto_market_analysis import recovery
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.evidence import archive_evidence, canonical_json, content_hash, restore_evidence
from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset, RESTCapture
from crypto_market_analysis.journal import AssessmentJournal
from crypto_market_analysis.models import Candle
from crypto_market_analysis.storage import Store


def _resign(record):
    """Build internally hashed fixtures to test checks beyond the outer checksum."""
    result = deepcopy(record)
    result["content_hash"] = content_hash({"payload": result["payload"], "evidence": result["evidence"]})
    result["assessment_id"] = "assessment_" + content_hash([result["idempotency_key"], result["content_hash"]])
    for key in ("config", "rules"):
        result["provenance"][key] = result["payload"][key]
        result["provenance"][key + "_hash"] = content_hash(result["payload"][key])
    return result


@pytest.fixture
def issued(tmp_path, monkeypatch):
    now = 4 * 86400 + 1800
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: now)
    engine = Engine(Settings(data_dir=tmp_path / "source", market_interval=1800))
    bars = [Candle("fixture", "BTC", "EUR", "1h", start, start + 3600,
                   10, 10, 10, 10, 1, start + 3600) for start in range(2 * 86400, 4 * 86400, 3600)]
    engine.store.put_candles(bars)
    # An older consumed version exercises the distinct plan versus capture hashes.
    engine.store.put_candles([replace(bars[0], open=20, high=20, low=20, close=20, observed_at=4 * 86400 - 1)])
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "start_time": 3 * 86400, "contributions": [{"time": 3 * 86400, "amount": "100"}],
              "schedule": [{"time": 3 * 86400, "amount": "90"}], "fees": {"fixed": "1"}}
    record = engine.issue_assessment("synthetic-recovery", "BTC", "fixture", "EUR", 4 * 86400, config)
    return engine, record


def test_checked_replay_is_independent_of_storage_and_current_clock(issued, monkeypatch):
    engine, record = issued
    baseline = deepcopy(record)
    with engine.store.connection() as con:
        con.execute("DELETE FROM candles")
    engine.store.path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Replay must not open a store, sample a clock or append outcomes.")

    monkeypatch.setattr("crypto_market_analysis.storage.Store.__init__", forbidden)
    monkeypatch.setattr("crypto_market_analysis.application.time.time", forbidden)
    monkeypatch.setattr(AssessmentJournal, "append_outcome", forbidden)
    result = recovery.replay_assessment(record)
    assert result["status"] == "verified"
    assert result["scope"] == "original_calculations_only_no_future_outcomes"
    assert result["issued_at"] != result["as_of"]
    for key in ("context", "windows", "plan", "input_reference"):
        assert result[key] == record["payload"][key]
    assert result["plan"]["input_reference"]["content_hash"] != result["input_reference"]["content_hash"]
    assert result["plan"]["plans"]["regular"]["terminals"]["90"]["status"] == "pending"
    assert record == baseline
    assert not engine.store.path.exists()


@pytest.mark.parametrize("part,message", [
    ("context", "context calculation mismatch"),
    ("windows", "purchase-window calculation mismatch"),
    ("plan", "plan calculation mismatch"),
])
def test_replay_checks_outputs_even_when_outer_envelope_hashes_match(issued, part, message):
    _, record = issued
    record["payload"][part]["fabricated"] = "not part of the frozen calculation"
    with pytest.raises(ValueError, match=message):
        recovery.replay_assessment(_resign(record))


@pytest.mark.parametrize("part", ["context", "purchase_windows", "plan", "evidence", "capture"])
def test_unsupported_frozen_versions_fail_explicitly(issued, part):
    _, record = issued
    if part in ("context", "purchase_windows"):
        record["payload"]["rules"][part] = 999
    elif part == "plan":
        record["payload"]["plan"]["rule_version"] = "accumulation-v1"
    elif part == "evidence":
        record["evidence"]["schema_version"] = 999
    else:
        record["payload"]["input_reference"]["rule_version"] = "captured-inputs-999"
    with pytest.raises(ValueError, match="[Uu]nsupported"):
        recovery.replay_assessment(_resign(record))


def test_raw_legacy_evidence_and_full_legacy_plan_vintages_replay(issued):
    _, record = issued
    evidence = restore_evidence(record["evidence"])
    all_rows = sorted(evidence["plan_candle_versions"] + evidence["candles"],
                      key=lambda row: (row["open_time"], row["observed_at"]))
    evidence["plan_candle_versions"] = all_rows
    del evidence["plan_uses_captured_candles"]
    record["evidence"] = evidence
    assert recovery.replay_assessment(_resign(record))["status"] == "verified"


def test_future_plan_replays_without_inventing_future_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: 3600)
    engine = Engine(Settings(data_dir=tmp_path))
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "start_time": 7200, "contributions": [], "schedule": []}
    record = engine.issue_assessment("future-replay", "BTC", "fixture", "EUR", plan_config=config)
    result = recovery.replay_assessment(record)
    assert result["plan"] == record["payload"]["plan"]
    assert result["plan"]["status"] == "not_started"
    assert engine.assessment_outcomes(record["assessment_id"]) == []


def test_backup_verify_and_restore_preserve_journal_without_initialization(issued, tmp_path):
    engine, record = issued
    engine.observe_assessment(record["assessment_id"], record["issued_at"])
    bundle = tmp_path / "backup"
    result = recovery.create_backup(engine.store.path, bundle)
    assert result["verification"]["originals_verified"] == 1
    assert result["verification"]["outcomes_verified"] == 1
    assert result["manifest"]["snapshot_method"] == "sqlite_online_backup"
    assert result["manifest"]["build"]["runtime_packaged"] is False
    before = {p.name: p.read_bytes() for p in bundle.iterdir()}
    assert recovery.verify_backup(bundle)["status"] == "verified"
    assert {p.name: p.read_bytes() for p in bundle.iterdir()} == before
    assert not list(bundle.glob("*-wal")) and not list(bundle.glob("*-shm"))
    destination = tmp_path / "restored"
    destination.mkdir()
    restored = recovery.restore_backup(bundle, destination)
    assert restored["status"] == "restored"
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before
    # Open the restored snapshot without calling Store's schema initializer.
    with sqlite3.connect(f"file:{restored['database']}?mode=ro", uri=True) as con:
        actual = json.loads(con.execute("SELECT record FROM assessment_journal").fetchone()[0])
    assert actual == record
    assert recovery.replay_assessment(actual)["status"] == "verified"


def test_online_backup_includes_committed_wal_and_consistent_concurrent_transactions(tmp_path):
    path = tmp_path / "source" / "observer.sqlite"
    store = Store(path)
    with store.connection() as con:
        con.execute("CREATE TABLE paired_rows (batch INTEGER, side INTEGER, padding BLOB)")
    started, stop = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            with sqlite3.connect(path, timeout=15) as con:
                con.execute("PRAGMA wal_autocheckpoint=0")
                for batch in range(200):
                    con.execute("INSERT INTO paired_rows VALUES(?,0,zeroblob(8192))", (batch,))
                    con.execute("INSERT INTO paired_rows VALUES(?,1,zeroblob(8192))", (batch,))
                    con.commit()
                    started.set()
                    if stop.is_set():
                        break
                stop.wait(10)
        except Exception as exc:
            errors.append(exc)
            started.set()

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        assert started.wait(10)
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        result = recovery.create_backup(path, tmp_path / "snapshot")
        with sqlite3.connect(result["database"]) as con:
            counts = con.execute("SELECT batch,COUNT(*),SUM(side) FROM paired_rows GROUP BY batch").fetchall()
            assert counts
            assert all((count, sides) == (2, 1) for _, count, sides in counts)
        assert recovery.verify_backup(tmp_path / "snapshot")["verification"]["integrity"] == "ok"
    finally:
        stop.set()
        worker.join(15)
    assert not worker.is_alive()
    assert not errors


@pytest.mark.parametrize("file", ["observer.sqlite", "uv.lock", "pyproject.toml"])
def test_checksum_tampering_is_rejected_before_restore(tmp_path, file):
    store = Store(tmp_path / "source.sqlite")
    bundle = tmp_path / "backup"
    recovery.create_backup(store.path, bundle)
    path = bundle / file
    if not path.exists():
        pytest.skip("Build metadata is unavailable in this installation.")
    path.write_bytes(path.read_bytes() + b"tampered")
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="checksum or size mismatch"):
        recovery.restore_backup(bundle, destination)
    assert not destination.exists()


def test_archive_corruption_is_rejected_even_if_journal_is_rehashed(issued, tmp_path):
    engine, record = issued
    record["evidence"]["sha256"] = "0" * 64
    record = _resign(record)
    with engine.store.connection() as con:
        con.execute("DROP TRIGGER assessment_journal_no_update")
        con.execute("UPDATE assessment_journal SET assessment_id=?,content_hash=?,record=?",
                    (record["assessment_id"], record["content_hash"], canonical_json(record)))
        con.executescript("""CREATE TRIGGER assessment_journal_no_update BEFORE UPDATE ON assessment_journal
                            BEGIN SELECT RAISE(ABORT,'Immutable assessment'); END;""")
    destination = tmp_path / "backup"
    with pytest.raises(ValueError, match="checksum or size mismatch"):
        recovery.create_backup(engine.store.path, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".backup-staging-*"))


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_publication_failure_rolls_back_staging_and_preserves_empty_target(tmp_path, monkeypatch, operation):
    store = Store(tmp_path / "source.sqlite")
    bundle = tmp_path / "backup"
    recovery.create_backup(store.path, bundle)
    destination = tmp_path / "failed"
    destination.mkdir()

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(recovery.os, "rename", fail)
    with pytest.raises(OSError, match="publication failure"):
        if operation == "backup":
            recovery.create_backup(store.path, destination)
        else:
            recovery.restore_backup(bundle, destination)
    assert destination.is_dir() and not list(destination.iterdir())
    assert not list(tmp_path.glob(".failed-staging-*"))
    assert Store(store.path).counts()["candles"] == 0


def test_existing_stores_and_destination_symlinks_are_never_overwritten(tmp_path):
    store = Store(tmp_path / "source" / "observer.sqlite")
    bundle = tmp_path / "backup"
    recovery.create_backup(store.path, bundle)
    source_before = store.path.read_bytes()
    for destination in (store.path, store.path.parent, bundle):
        with pytest.raises(ValueError, match="empty directory"):
            recovery.restore_backup(bundle, destination)
        with pytest.raises(ValueError, match="empty directory"):
            recovery.create_backup(store.path, destination)
    empty = tmp_path / "empty"
    empty.mkdir()
    link = tmp_path / "symlink"
    link.symlink_to(empty, target_is_directory=True)
    with pytest.raises(ValueError, match="empty directory"):
        recovery.restore_backup(bundle, link)
    assert not list(empty.iterdir())
    assert store.path.read_bytes() == source_before


@pytest.mark.parametrize("marker", ["dataset.json", "history_captures"])
def test_incomplete_or_unconfirmed_named_datasets_are_rejected(tmp_path, marker):
    store = Store(tmp_path / "source" / "observer.sqlite")
    if marker == "dataset.json":
        (store.path.parent / marker).write_text("{}")
    else:
        with store.connection() as con:
            con.execute("CREATE TABLE history_captures (payload TEXT)")
    with pytest.raises(ValueError, match="quiescent_dataset|dataset.json identity"):
        recovery.create_backup(store.path, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_missing_sources_and_future_database_schemas_are_not_created_or_migrated(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="existing SQLite"):
        recovery.create_backup(missing, tmp_path / "backup")
    assert not missing.exists()
    store = Store(tmp_path / "future.sqlite")
    with store.connection() as con:
        con.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="Unsupported SQLite store schema version"):
        recovery.create_backup(store.path, tmp_path / "backup")
    with sqlite3.connect(store.path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 999


def test_unknown_calculation_rules_remain_preservable_without_claiming_replay(issued, tmp_path):
    engine, record = issued
    record["payload"]["plan"]["rule_version"] = "accumulation-v1"
    old = _resign(record)
    journal = AssessmentJournal(engine.store)
    journal.issue("old-rule-preserved", old["payload"], old["evidence"], old["issued_at"])
    result = recovery.create_backup(engine.store.path, tmp_path / "backup")
    assert result["verification"]["originals_verified"] == 2
    with pytest.raises(ValueError, match="Unsupported frozen plan rule"):
        recovery.replay_assessment(old)


def test_capture_and_plan_inputs_are_checked_independently(issued):
    _, record = issued
    evidence = restore_evidence(record["evidence"])
    evidence["plan_candle_versions"][0]["close"] = 9
    record["evidence"] = archive_evidence(evidence)
    with pytest.raises(ValueError, match="plan input hash mismatch"):
        recovery.replay_assessment(_resign(record))


def _named_dataset(tmp_path):
    config = DatasetConfig("synthetic-recovery", str(tmp_path / "research"), 0, 7200)
    dataset = HistoryDataset(config, clock=lambda: 14400)
    capture = dataset.append_rest("BTC", "EUR", lambda *_: RESTCapture(
        [[0, 10, 10, 10, 10, 2]], b"synthetic-exact-raw-response"), since=0)
    dataset.plan_repair("BTC", "EUR", 0, 7200)
    return dataset, capture


def test_named_dataset_backup_restores_portable_identity_payloads_and_audit(tmp_path):
    dataset, capture = _named_dataset(tmp_path)
    # An unreferenced content-addressed blob is deliberately excluded.
    unused = b"unreferenced synthetic payload"
    import hashlib

    (dataset.directory / (hashlib.sha256(unused).hexdigest() + ".payload")).write_bytes(unused)
    original_identity = (dataset.directory / "dataset.json").read_bytes()
    result = recovery.create_backup(dataset.store.path, tmp_path / "backup", quiescent_dataset=True)
    files = result["manifest"]["files"]
    assert capture["normalized_sha256"] + ".payload" in files
    assert capture["raw_sha256"] + ".payload" in files
    assert hashlib.sha256(unused).hexdigest() + ".payload" not in files
    restored = recovery.restore_backup(tmp_path / "backup", tmp_path / "restored-研究")
    metadata = json.loads((tmp_path / "restored-研究" / "dataset.json").read_text())
    assert metadata["directory"] == str(tmp_path / "restored-研究")
    assert restored["manifest"]["dataset"]["original_identity"]["directory"] == str(dataset.directory)
    assert restored["manifest"]["dataset"]["directory_rebased"] is True
    reopened = HistoryDataset(DatasetConfig(**metadata), clock=lambda: 14400)
    manifest = reopened.manifest()
    assert manifest["captures"][0] == dataset.manifest()["captures"][0]
    assert manifest["markets"][0]["repair"] == dataset.manifest()["markets"][0]["repair"]
    assert reopened.store.candles("BTC", "kraken", "EUR", 14400, "import") == dataset.store.candles(
        "BTC", "kraken", "EUR", 14400, "import")
    assert (dataset.directory / "dataset.json").read_bytes() == original_identity
    engine = Engine(Settings(data_dir=tmp_path / "restored-研究"), "import")
    assert engine.dataset_lineage["name"] == dataset.config.name


def test_named_dataset_pending_capture_is_portable_and_recovers_once(tmp_path):
    dataset, capture = _named_dataset(tmp_path)
    # Model an interruption after the payload/pending journal was fsynced but
    # before the corresponding SQLite transaction. The source is left untouched.
    pending = deepcopy(capture)
    pending["id"] = "a" * 64
    pending_path = dataset.directory / ("pending-" + pending["id"] + ".json")
    pending_path.write_text(canonical_json(pending))
    recovery.create_backup(dataset.store.path, tmp_path / "backup", quiescent_dataset=True)
    result = recovery.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert pending_path.exists()
    config = DatasetConfig(**result["manifest"]["dataset"]["identity"])
    restored = HistoryDataset(config, clock=lambda: 14400)
    assert not list(restored.directory.glob("pending-*.json"))
    assert len(restored.manifest()["captures"]) == 2
    assert len(HistoryDataset(config, clock=lambda: 14400).manifest()["captures"]) == 2


@pytest.mark.parametrize("change", ["database", "file"])
def test_named_dataset_mutation_during_copy_aborts_publication(tmp_path, monkeypatch, change):
    dataset, _ = _named_dataset(tmp_path)
    copyfile = recovery.shutil.copyfile
    changed = False

    def interleaved_copy(source, target):
        nonlocal changed
        result = copyfile(source, target)
        if not changed:
            changed = True
            if change == "database":
                with dataset.store.connection() as con:
                    con.execute("INSERT INTO history_attempts(asset,quote,payload) VALUES('BTC','EUR','{}')")
            else:
                path = dataset.directory / "dataset.json"
                path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(recovery.shutil, "copyfile", interleaved_copy)
    with pytest.raises(ValueError, match="changed during backup"):
        recovery.create_backup(dataset.store.path, tmp_path / "backup", quiescent_dataset=True)
    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".backup-staging-*"))


@pytest.mark.parametrize("limit", [{"max_dataset_files": 1}, {"max_dataset_bytes": 1}])
def test_named_dataset_copy_resource_bounds_are_explicit(tmp_path, limit):
    dataset, _ = _named_dataset(tmp_path)
    with pytest.raises(ValueError, match="configured file or byte bound"):
        recovery.create_backup(dataset.store.path, tmp_path / "backup", quiescent_dataset=True, **limit)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("corruption", ["missing", "hash", "symlink"])
def test_named_dataset_missing_corrupt_or_linked_payloads_fail_safely(tmp_path, corruption):
    dataset, capture = _named_dataset(tmp_path)
    payload = dataset.directory / (capture["normalized_sha256"] + ".payload")
    payload.unlink()
    if corruption == "hash":
        payload.write_bytes(b"corrupted normalized rows")
    elif corruption == "symlink":
        external = tmp_path / "external"
        external.write_bytes(b"synthetic")
        payload.symlink_to(external)
    with pytest.raises(ValueError, match="missing required|content-addressed|symlinks"):
        recovery.create_backup(dataset.store.path, tmp_path / "backup", quiescent_dataset=True)
    assert not (tmp_path / "backup").exists()
