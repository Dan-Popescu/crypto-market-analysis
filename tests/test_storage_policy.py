"""Lossless bounded retention checks use temporary synthetic SQLite stores only."""
import hashlib
import json
from dataclasses import replace

import pytest

from crypto_market_analysis.book_snapshots import read_books, retain_books, retained_book
from crypto_market_analysis.cli import main
from crypto_market_analysis.journal import AssessmentJournal
from crypto_market_analysis.models import CollectionResult, Observation
from crypto_market_analysis.recovery import create_backup, restore_backup, verify_backup
from crypto_market_analysis.storage import Store
from crypto_market_analysis.storage_policy import (
    StoragePolicy,
    archive_directory,
    archive_rows,
    inspect_storage,
    maintain_storage,
    physical_storage,
    read_archived_books,
    restore_archive,
    verify_archive,
)

DAY = 86400


def book(at=1, source="kraken", event=1000):
    return retained_book({"timestamp": event, "bids": [["99", "1", 3]], "asks": [["101", "2"]]},
                         source=source, asset="BTC", quote="EUR", symbol="BTC/EUR", captured_at=at,
                         requested_limit=500, provenance={"provider": "synthetic-offline-test"})


def selectors():
    return {"sources": ["kraken"], "asset": "BTC", "quote": "EUR", "data_mode": "live", "as_of": 100}


def archive_one(store, **changes):
    policy = replace(StoragePolicy(), **changes)
    return maintain_storage(store, policy, apply=True, now=10 * DAY)["families"][0]["archive"]


def test_default_dry_run_does_not_write_or_delete(tmp_path, capsys):
    store = Store(tmp_path / "observer.sqlite")
    retain_books(store, [book()])
    before = store.path.read_bytes()
    result = maintain_storage(store, now=10 * DAY)
    assert result["apply"] is False and result["families"][0]["eligible_batch_rows"] == 1
    assert read_books(store, **selectors()) == [book()]
    assert not archive_directory(store.path).exists()
    assert store.path.read_bytes() == before
    assert main(["--data-dir", str(tmp_path), "maintain"]) == 0
    assert json.loads(capsys.readouterr().out)["apply"] is False
    assert not archive_directory(store.path).exists()


def test_verified_archive_roundtrip_preserves_exact_book_and_unknown_clock(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    captures = [book(), book(at=2, source="coinbase", event=None)]
    retain_books(store, captures)
    path = archive_one(store)
    checked = verify_archive(path)
    assert checked["status"] == "verified" and len(checked["rows"]) == 2
    assert read_books(store, **selectors()) == []
    archived = read_archived_books(store.path, **{**selectors(), "sources": ["kraken", "coinbase"]})
    assert archived["books"] == captures and archived["storage_tier"] == "archive"
    assert archived["books"][1]["event_time"] is None
    assert restore_archive(path, store)["inserted_rows"] == 0
    assert restore_archive(path, store, apply=True)["inserted_rows"] == 2
    assert restore_archive(path, store, apply=True)["inserted_rows"] == 0
    assert read_books(store, **selectors()) == [captures[0]]


def test_interruption_after_archive_before_delete_reuses_original_file(tmp_path, monkeypatch):
    store = Store(tmp_path / "observer.sqlite")
    retain_books(store, [book()])
    import crypto_market_analysis.storage_policy as module
    publish = module.archive_rows

    def interrupted(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("crash before hot deletion")

    monkeypatch.setattr(module, "archive_rows", interrupted)
    with pytest.raises(RuntimeError, match="before hot deletion"):
        archive_one(store)
    assert read_books(store, **selectors()) == [book()]
    monkeypatch.setattr(module, "archive_rows", publish)
    path = archive_one(store)
    assert verify_archive(path)["rows"][0]["payload"]
    assert len(list(archive_directory(store.path).iterdir())) == 1
    assert read_books(store, **selectors()) == []


def test_interrupted_partial_staging_resumes_without_losing_originals(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    retain_books(store, [book()])
    with store.connection() as con:
        rows = [dict(row) for row in con.execute("SELECT * FROM book_snapshots")]
    encoded = json.dumps({"schema_version": 1, "family": "book_snapshots", "rows": rows},
                         sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    directory = archive_directory(store.path)
    directory.mkdir()
    pending = directory / (hashlib.sha256(encoded).hexdigest() + ".pending")
    pending.write_bytes(encoded[:25])
    target = archive_rows(store, "book_snapshots", rows)
    assert not pending.exists() and verify_archive(target)["rows"] == rows
    assert read_books(store, **selectors()) == [book()]


def test_concurrent_late_capture_is_not_deleted_by_snapshot_batch(tmp_path, monkeypatch):
    store = Store(tmp_path / "observer.sqlite")
    other = Store(store.path)
    retain_books(store, [book()])
    import crypto_market_analysis.storage_policy as module
    publish = module.archive_rows

    def interleave(*args, **kwargs):
        path = publish(*args, **kwargs)
        retain_books(other, [book(at=2)])
        return path

    monkeypatch.setattr(module, "archive_rows", interleave)
    result = maintain_storage(store, apply=True, now=10 * DAY)
    assert result["families"][0]["deleted_rows"] == 1
    assert read_books(store, **selectors()) == [book(at=2)]


def test_archive_exact_byte_quota_and_corruption_fail_closed(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    retain_books(store, [book()])
    with store.connection() as con:
        rows = [dict(row) for row in con.execute("SELECT * FROM book_snapshots")]
    path = archive_rows(store, "book_snapshots", rows)
    actual = physical_storage(store.path)["archive_bytes"]
    assert actual == path.stat().st_size
    assert archive_rows(store, "book_snapshots", rows, replace(StoragePolicy(), archive_max_bytes=actual)) == path
    retain_books(store, [book(at=2)])
    with pytest.raises(ValueError, match="Archive byte budget"):
        archive_one(store, archive_max_bytes=actual)
    assert read_books(store, **selectors()) == [book(at=2)]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        verify_archive(path)
    with pytest.raises(ValueError):
        restore_archive(path, store, apply=True)


def test_bounded_batches_preserve_latest_runs_and_all_journal_originals(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    journal = AssessmentJournal(store)
    issued = journal.issue("request", {"synthetic": True}, {}, issued_at=1)
    outcome = journal.append_outcome(issued["assessment_id"], "outcome", {"synthetic": True}, observed_at=2)
    retain_books(store, [book(at=at) for at in range(1, 5)])
    for at in (1, 2, 3):
        store.record_run(CollectionResult("kraken"), "options", at)
    result = maintain_storage(store, replace(StoragePolicy(), batch_rows=2), apply=True, now=40 * DAY)
    assert [item["deleted_rows"] for item in result["families"]] == [2, 2]
    assert journal.get(issued["assessment_id"]) == issued
    assert journal.outcomes(issued["assessment_id"]) == [outcome]
    assert store.runs()[0]["observed_at"] == 3


def test_hot_wal_guard_backpressure_keeps_reads_and_idempotent_retries(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    capture = book()
    observation = Observation("kraken", "BTC", "price", 1, 1, 100, "EUR", "EUR")
    retain_books(store, [capture])
    store.put_observations([observation])
    store.storage_policy = replace(StoragePolicy(), hot_max_bytes=1)
    assert retain_books(store, [capture]) == 0
    assert store.put_observations([observation]) == 0
    assert read_books(store, **selectors()) == [capture]
    with pytest.raises(ValueError, match="Hot SQLite/WAL"):
        retain_books(store, [book(at=2)])
    assert archive_one(store)
    assert read_books(store, **selectors()) == []


def test_backup_restore_includes_verified_retention_archives(tmp_path):
    store = Store(tmp_path / "source" / "observer.sqlite")
    retain_books(store, [book()])
    archive_one(store)
    bundle = tmp_path / "backup"
    create_backup(store.path, bundle)
    assert verify_backup(bundle)["manifest"]["retention_archives"]
    restored = restore_backup(bundle, tmp_path / "restored")
    assert read_archived_books(restored["database"], **selectors())["books"] == [book()]


def test_inspection_reports_actual_utf8_payload_and_physical_files(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    retain_books(store, [book()])
    status = inspect_storage(store.path)
    assert status["physical"]["database_bytes"] == store.path.stat().st_size
    assert status["families"]["book_snapshots"]["payload_bytes"] > 0
    assert status["limits"]["kind"] == "admission_budgets_not_filesystem_quota"
