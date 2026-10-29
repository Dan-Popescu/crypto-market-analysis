"""Real SIGKILL recovery at observed local commit boundaries; no power-loss claim.

Children use tiny explicitly synthetic/import or demo captures and deny network.
Every rendezvous and cleanup has a wall-clock deadline; hooks run only in children.
"""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from crypto_market_analysis.automatic_repair import (
    AutoRepairRequest,
    automatic_repair_status,
    plan_auto_repairs,
    run_auto_repairs,
)
from crypto_market_analysis.book_snapshots import retain_books, retained_book
from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset, RESTCapture
from crypto_market_analysis.models import Observation
from crypto_market_analysis.retention import compact_liquidity
from crypto_market_analysis.storage import Store
from crypto_market_analysis.storage_policy import archive_directory, maintain_storage, verify_archive

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX SIGKILL and pipe selectors")
HOUR = 3600
DAY = 86400
NOW = 1000 * HOUR
RAW = b'{"fixture":"synthetic process-crash response"}'
REQUEST = AutoRepairRequest(markets=(("BTC", "EUR"),), max_hours=3, request_budget=1)


def _config(directory):
    return DatasetConfig("synthetic-process-crash", str(directory), 997 * HOUR, 1000 * HOUR)


def _row(hour):
    return [hour * HOUR * 1000, 10, 12, 9, 11, 2]


def _capture(hours):
    return RESTCapture([_row(h) for h in hours], raw=RAW,
                       capture_evidence="injected_synthetic_process_crash")


def _rows(database, table):
    # Inspect independently without Store initialization, migration or recovery.
    with closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def _rendezvous(scenario):
    print(json.dumps({"boundary": scenario}), flush=True)
    # The parent kills this process. If the parent fails, the child exits itself.
    Event().wait(30)
    raise RuntimeError("Parent did not kill child within rendezvous deadline")


@contextmanager
def _child_at_boundary(scenario, directory):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", scenario,
                                str(directory)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), f"Child did not reach {scenario} within 10 seconds"
            line = process.stdout.readline()
        if not line:
            _, error = process.communicate(timeout=5)
            pytest.fail(f"Child exited before {scenario}: {error}")
        assert json.loads(line) == {"boundary": scenario}
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def _kill(process):
    process.kill()
    assert process.wait(timeout=5) == -signal.SIGKILL


def _book():
    return retained_book({"timestamp": 1000, "bids": [["99", "1"]], "asks": [["101", "2"]]},
                         source="synthetic-offline", asset="BTC", quote="EUR", symbol="BTC/EUR",
                         captured_at=2, requested_limit=5, data_mode="demo",
                         provenance={"fixture": "synthetic process-crash evidence"})


def _child(scenario, directory):
    def denied(*_args, **_kwargs):
        raise AssertionError("Network forbidden in process-crash test")

    with patch("socket.socket.connect", denied), patch("socket.create_connection", denied), \
            patch("socket.socket.connect_ex", denied), patch("socket.getaddrinfo", denied), \
            patch("socket.socket.sendto", denied):
        if scenario == "pending_capture":
            dataset = HistoryDataset(_config(directory), clock=lambda: NOW)
            with patch.object(Store, "put_candles", lambda *_: _rendezvous(scenario)):
                dataset.append_rest("BTC", "EUR", lambda *_: _capture([997, 999]), since=997 * HOUR)
        elif scenario in ("materialized_complete", "materialized_partial"):
            original = HistoryDataset.append_rest

            def captured(self, *args, **kwargs):
                original(self, *args, **kwargs)
                _rendezvous(scenario)

            hours = [997, 998, 999] if scenario == "materialized_complete" else [997, 999]
            with patch.object(HistoryDataset, "append_rest", captured):
                run_auto_repairs(directory / "observer.sqlite", REQUEST, apply=True,
                                 fetch=lambda *_: _capture(hours), clock=lambda: NOW)
        elif scenario.startswith("archive_"):
            import crypto_market_analysis.storage_policy as policy

            store = Store(directory / "observer.sqlite")
            if scenario == "archive_pending":
                original = policy.verify_archive

                def checked(path):
                    result = original(path)
                    if path.suffix == ".pending":
                        _rendezvous(scenario)
                    return result

                with patch.object(policy, "verify_archive", checked):
                    maintain_storage(store, apply=True, now=10 * DAY)
            elif scenario == "archive_published":
                original = policy.archive_rows

                def published(*args, **kwargs):
                    original(*args, **kwargs)
                    _rendezvous(scenario)

                with patch.object(policy, "archive_rows", published):
                    maintain_storage(store, apply=True, now=10 * DAY)
            else:
                original = Store.connection

                class BeforeCommit:
                    def __init__(self, con):
                        self.con = con

                    def __getattr__(self, name):
                        return getattr(self.con, name)

                    def execute(self, sql, *args):
                        result = self.con.execute(sql, *args)
                        if sql.startswith('DELETE FROM "book_snapshots"'):
                            assert self.con.in_transaction and result.rowcount == 1
                            _rendezvous(scenario)
                        return result

                @contextmanager
                def connection(self):
                    with original(self) as con:
                        yield BeforeCommit(con)

                with patch.object(Store, "connection", connection):
                    maintain_storage(store, apply=True, now=10 * DAY)
        elif scenario == "retention_aggregate":
            original = Store.put_observations

            def aggregated(self, rows):
                original(self, rows)
                _rendezvous(scenario)

            with patch.object(Store, "put_observations", aggregated):
                compact_liquidity(Store(directory / "observer.sqlite"), now=20 * DAY)
        else:
            raise ValueError(f"Unknown crash scenario: {scenario}")
    raise AssertionError("Child did not rendezvous")


def test_sigkill_pending_capture_read_only_then_exact_idempotent_recovery(tmp_path):
    config = _config(tmp_path / "research")
    dataset = HistoryDataset(config, clock=lambda: NOW)
    with _child_at_boundary("pending_capture", dataset.directory) as process:
        journal = next(dataset.directory.glob("pending-*.json"))
        encoded = journal.read_bytes()
        expected = json.loads(encoded)
        blobs = {key: (dataset.directory / (expected[key] + ".payload")).read_bytes()
                 for key in ("normalized_sha256", "raw_sha256")}
        assert all(hashlib.sha256(value).hexdigest() == expected[key] for key, value in blobs.items())
        assert blobs["raw_sha256"] == RAW
        assert _rows(dataset.store.path, "history_captures") == []
        assert _rows(dataset.store.path, "candles") == []
        _kill(process)
    before = {name: _rows(dataset.store.path, name) for name in ("candles", "history_captures")}
    assert plan_auto_repairs(dataset.store.path, REQUEST, now=NOW)["markets"][0]["remaining_hours"] == 3
    assert automatic_repair_status(dataset.store.path)["status"] == "not_recorded"
    assert journal.read_bytes() == encoded
    assert before == {name: _rows(dataset.store.path, name) for name in before}

    reopened = HistoryDataset(config, clock=lambda: NOW + 10)
    assert not journal.exists()
    captures = _rows(reopened.store.path, "history_captures")
    assert len(captures) == 1 and json.loads(captures[0]["payload"]) == expected
    candles = reopened.store.candles("BTC", quote="EUR", mode="import", as_of=NOW + 10)
    assert [c.open_time for c in candles] == [997 * HOUR, 999 * HOUR]
    assert all(c.observed_at == NOW for c in candles)
    assert [asdict(c) for c in candles] == json.loads(blobs["normalized_sha256"])
    assert expected["captured_at"] == expected["materialized_at"] == NOW
    assert expected["raw_response_sha256"] == expected["raw_sha256"]
    assert expected["capture_evidence"] == "injected_synthetic_process_crash"
    rows = _rows(reopened.store.path, "candles")
    HistoryDataset(config, clock=lambda: NOW + 20)
    assert _rows(reopened.store.path, "history_captures") == captures
    assert _rows(reopened.store.path, "candles") == rows
    assert all((dataset.directory / (expected[key] + ".payload")).read_bytes() == value
               for key, value in blobs.items())


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "partial"])
def test_sigkill_materialized_capture_reconciles_only_actual_missing_hours(tmp_path, complete):
    dataset = HistoryDataset(_config(tmp_path / "research"), clock=lambda: NOW)
    scenario = "materialized_complete" if complete else "materialized_partial"
    with _child_at_boundary(scenario, dataset.directory) as process:
        state = automatic_repair_status(dataset.store.path)["tasks"][0]
        assert state["status"] == "inflight" and state["requests"] == 1
        captures = _rows(dataset.store.path, "history_captures")
        original_rows = _rows(dataset.store.path, "candles")
        assert len(captures) == 1 and len(original_rows) == (3 if complete else 2)
        assert not list(dataset.directory.glob("pending-*.json"))
        _kill(process)
    seen = []

    def fetch(_asset, _quote, since, limit):
        seen.append((since, limit))
        return _capture([since // HOUR + n for n in range(limit)])

    result = run_auto_repairs(dataset.store.path, REQUEST, apply=True, fetch=fetch, clock=lambda: NOW + 60)
    assert seen == ([] if complete else [(998 * HOUR, 1)])
    assert result["requests_attempted"] == (0 if complete else 1)
    final = automatic_repair_status(dataset.store.path)["tasks"][0]
    assert final["status"] == "complete" and final["remaining_hours"] == 0
    assert final["requests"] == (1 if complete else 2)
    assert result["markets"][0]["remaining_hours"] == 0
    assert captures[0] in _rows(dataset.store.path, "history_captures")
    recovered = _rows(dataset.store.path, "candles")
    assert all(row in recovered for row in original_rows)
    assert len(recovered) == 3
    again = run_auto_repairs(dataset.store.path, REQUEST, apply=True,
                             fetch=lambda *_: pytest.fail("Completed recovery fetched again"),
                             clock=lambda: NOW + 120)
    assert again["requests_attempted"] == 0
    assert _rows(dataset.store.path, "candles") == recovered


@pytest.mark.parametrize("scenario", ["archive_pending", "archive_published", "archive_uncommitted_delete"])
def test_sigkill_archive_restart_preserves_exact_evidence_and_rolls_back_delete(tmp_path, scenario):
    store = Store(tmp_path / "observer.sqlite")
    retain_books(store, [_book()])
    original_rows = _rows(store.path, "book_snapshots")
    with _child_at_boundary(scenario, tmp_path) as process:
        paths = list(archive_directory(store.path).iterdir())
        assert len(paths) == 1
        path = paths[0]
        checked = verify_archive(path)
        encoded = path.read_bytes()
        assert checked["rows"] == original_rows
        assert path.suffix == (".pending" if scenario == "archive_pending" else ".json")
        # Also true with a DELETE already executed in the child's uncommitted writer.
        assert _rows(store.path, "book_snapshots") == original_rows
        _kill(process)
    assert _rows(store.path, "book_snapshots") == original_rows
    result = maintain_storage(store, apply=True, now=10 * DAY)
    assert result["families"][0]["deleted_rows"] == 1
    published = Path(result["families"][0]["archive"])
    assert published.read_bytes() == encoded
    assert verify_archive(published)["rows"] == original_rows
    assert _rows(store.path, "book_snapshots") == []
    assert list(archive_directory(store.path).iterdir()) == [published]
    assert maintain_storage(store, apply=True, now=10 * DAY)["families"] == []


def test_sigkill_retention_snapshot_before_pruning_does_not_recompute_median(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    store.put_observations([
        Observation("synthetic-offline", "BTC", "spread_bps", 10 * DAY + offset,
                    10 * DAY + offset, value, "bps", "EUR", {"coverage_complete": True}, "demo")
        for offset, value in ((10, 1), (20, 9), (30, 5))
    ])
    originals = _rows(store.path, "observations")
    with _child_at_boundary("retention_aggregate", tmp_path) as process:
        before = _rows(store.path, "observations")
        assert len(before) == 4 and all(row in before for row in originals)
        archives = list(archive_directory(store.path).glob("*.json"))
        assert len(archives) == 1 and verify_archive(archives[0])["rows"] == originals
        aggregate = next(row for row in before if row not in originals)
        payload = json.loads(aggregate["payload"])
        assert payload["value"] == 5 and payload["metadata"]["sample_count"] == 3
        _kill(process)
    result = compact_liquidity(store, now=20 * DAY + 60)
    assert result == {"aggregates_written": 0, "raw_rows_deleted": 3, "remaining_old_rows": 0}
    assert _rows(store.path, "observations") == [aggregate]
    assert verify_archive(archives[0])["rows"] == originals
    assert compact_liquidity(store, now=20 * DAY + 120)["raw_rows_deleted"] == 0


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--child":
        raise SystemExit("This test module only accepts --child SCENARIO DIRECTORY")
    _child(sys.argv[2], Path(sys.argv[3]))
