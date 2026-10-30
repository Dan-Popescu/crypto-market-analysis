"""Durable turns and real-process writer exclusion, entirely offline."""
import multiprocessing
from pathlib import Path

import pytest

from crypto_market_analysis.automatic_repair import (
    automatic_repair_status,
    plan_auto_repairs,
    run_auto_repairs,
)
from crypto_market_analysis.dataset_lock import dataset_writer_lock
from crypto_market_analysis.history_dataset import (
    MARKETS,
    DatasetConfig,
    HistoryDataset,
    RESTCapture,
    UnsupportedMarket,
)

HOUR = 3600
NOW = 1000 * HOUR
SMALL = {"request_budget": 1, "max_hours": 1}


def _dataset(tmp_path):
    return HistoryDataset(DatasetConfig("turns", str(tmp_path / "research"), 281 * HOUR, NOW),
                          clock=lambda: NOW)


def _rows(_asset, _quote, since, limit):
    return RESTCapture([[(since + n * HOUR) * 1000, 10, 12, 9, 11, 2] for n in range(limit)], raw=b"synthetic")


def _apply_child(database, now, queue, started=None, release=None, forbid_init=False):
    # A losing writer must not enter construction/recovery, even before fetch.
    if forbid_init:
        import crypto_market_analysis.automatic_repair as repair

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Busy writer entered writable dataset initialization")

        repair.HistoryDataset = forbidden

    def fetch(asset, quote, since, limit):
        if started is not None:
            started.set()
            if not release.wait(10):
                raise RuntimeError("Synthetic parent missed its bounded release deadline")
        return _rows(asset, quote, since, limit)

    try:
        result = run_auto_repairs(database, SMALL, apply=True, fetch=fetch, clock=lambda: now)
        queue.put(result)
    except BaseException as exc:
        queue.put({"child_error": type(exc).__name__})
        raise


def _stop(process):
    if process.is_alive():
        process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    assert not process.is_alive()


@pytest.mark.parametrize("bounds", [SMALL, {"request_budget": 4, "max_hours": 1},
                                     {"request_budget": 2, "max_hours": 2}])
def test_severe_budgets_rotate_all_native_markets_across_process_restarts(tmp_path, bounds):
    ds = _dataset(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    seen = []
    for cycle in range(8):
        # Plans are deterministic and inspecting never moves the durable turn.
        now = NOW + cycle * 60
        before = automatic_repair_status(ds.store.path)
        first = plan_auto_repairs(ds.store.path, bounds, now=now)
        assert plan_auto_repairs(ds.store.path, bounds, now=now) == first
        assert run_auto_repairs(ds.store.path, bounds, clock=lambda now=now: now) == first
        assert automatic_repair_status(ds.store.path) == before
        if bounds == SMALL:
            queue = ctx.Queue()
            process = ctx.Process(target=_apply_child, args=(str(ds.store.path), now, queue))
            process.start()
            try:
                result = queue.get(timeout=10)
                process.join(5)
                assert process.exitcode == 0
            finally:
                _stop(process)
                queue.close()
        else:
            result = run_auto_repairs(ds.store.path, bounds, apply=True, fetch=_rows, clock=lambda now=now: now)
        seen.extend((t["asset"], t["quote"]) for t in result["outcomes"])
        assert result["requests_attempted"] <= bounds["request_budget"]
    assert seen == list(MARKETS) * (len(seen) // 4)
    assert all(m["remaining_hours"] < 719 for m in result["markets"])


def test_rotation_excludes_manual_unsupported_and_retry_blocked_markets(tmp_path):
    ds = _dataset(tmp_path)
    ds.plan_repair("BTC", "EUR", 281 * HOUR, NOW)

    def unsupported(*_):
        raise UnsupportedMarket("synthetic unavailable market")

    run_auto_repairs(ds.store.path, {**SMALL, "markets": [["ETH", "EUR"]]},
                     apply=True, fetch=unsupported, clock=lambda: NOW)
    run_auto_repairs(ds.store.path, {**SMALL, "markets": [["BTC", "USD"]]},
                     apply=True, fetch=lambda *_: RESTCapture([]), clock=lambda: NOW)
    plan = plan_auto_repairs(ds.store.path, now=NOW)
    assert [(t["asset"], t["quote"]) for t in plan["tasks"]] == [("ETH", "USD")]
    result = run_auto_repairs(ds.store.path, SMALL, apply=True, fetch=_rows, clock=lambda: NOW)
    assert [(t["asset"], t["quote"]) for t in result["outcomes"]] == [("ETH", "USD")]
    # Once its retry deadline passes, the eligible USD market gets the next turn.
    assert [(t["asset"], t["quote"]) for t in plan_auto_repairs(ds.store.path, SMALL, now=NOW + 60)["tasks"]] == [("BTC", "USD")]


def test_dry_run_and_missing_apply_create_no_lock_or_cursor(tmp_path):
    missing = tmp_path / "absent" / "observer.sqlite"
    assert run_auto_repairs(missing, SMALL, apply=True, fetch=_rows, clock=lambda: NOW)["status"] == "not_configured"
    assert not missing.parent.exists()
    ds = _dataset(tmp_path)
    before = {p.name: p.read_bytes() for p in ds.directory.iterdir() if p.is_file()}
    run_auto_repairs(ds.store.path, SMALL, clock=lambda: NOW)
    assert not (ds.directory / ".history-writer.lock").exists()
    assert not automatic_repair_status(ds.store.path)["scheduling"]["recorded"]
    assert before == {p.name: p.read_bytes() for p in ds.directory.iterdir() if p.name in before}


@pytest.mark.parametrize("abrupt_kill", [False, True])
def test_process_lock_rejects_concurrent_apply_and_releases_after_exit(tmp_path, abrupt_kill):
    ds = _dataset(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    queue, loser_queue = ctx.Queue(), ctx.Queue()
    started, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_apply_child, args=(str(ds.store.path), NOW, queue, started, release))
    contender = ctx.Process(target=_apply_child, args=(str(ds.store.path), NOW, loser_queue),
                            kwargs={"forbid_init": True})
    holder.start()
    try:
        assert started.wait(10), "Writer never entered the bounded synthetic fetch"
        contender.start()
        result = loser_queue.get(timeout=10)
        contender.join(5)
        assert contender.exitcode == 0
        assert result["status"] == "busy" and result["reason"] == "dataset_writer_busy"
        assert result["applied"] is False and result["requests_attempted"] == 0
        state = automatic_repair_status(ds.store.path)
        assert state["tasks_total"] == 1 and state["tasks"][0]["requests"] == 1
        assert state["tasks"][0]["status"] == "inflight"
        assert state["scheduling"]["next_market"] == ["ETH", "EUR"]
        if abrupt_kill:
            holder.kill()
        else:
            release.set()
            assert queue.get(timeout=10)["requests_attempted"] == 1
        holder.join(5)
        assert not holder.is_alive()
        # Re-enter in this process after normal exit or SIGKILL. No stale-lock
        # expiry heuristic, PID check or manual lock-file deletion is needed.
        resumed = run_auto_repairs(ds.store.path, SMALL, apply=True, fetch=_rows, clock=lambda: NOW + 60)
        assert resumed["applied"] and resumed["requests_attempted"] == 1
        assert resumed["outcomes"][0]["asset"] == "ETH"
        assert (ds.directory / ".history-writer.lock").is_file()
    finally:
        if holder.is_alive():
            release.set()
        _stop(holder)
        if contender.pid is not None:
            _stop(contender)
        queue.close()
        loser_queue.close()


def test_base_exception_releases_guard_and_preserves_advanced_turn(tmp_path):
    ds = _dataset(tmp_path)

    def interrupted(*_):
        raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt):
        run_auto_repairs(ds.store.path, SMALL, apply=True, fetch=interrupted, clock=lambda: NOW)
    assert automatic_repair_status(ds.store.path)["scheduling"]["next_market"] == ["ETH", "EUR"]
    with dataset_writer_lock(ds.store.path):
        pass
    result = run_auto_repairs(ds.store.path, SMALL, apply=True, fetch=_rows, clock=lambda: NOW)
    assert (result["outcomes"][0]["asset"], result["outcomes"][0]["quote"]) == ("ETH", "EUR")


def test_lock_rejects_symlink_and_missing_store(tmp_path):
    with pytest.raises(ValueError, match="existing named"):
        with dataset_writer_lock(tmp_path / "missing" / "observer.sqlite"):
            pytest.fail("Missing store acquired lock")
    ds = _dataset(tmp_path)
    target = tmp_path / "external"
    target.write_bytes(b"unchanged")
    lock = Path(ds.directory) / ".history-writer.lock"
    try:
        lock.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable")
    with pytest.raises((ValueError, OSError)):
        with dataset_writer_lock(ds.store.path):
            pytest.fail("Symlink acquired lock")
    assert target.read_bytes() == b"unchanged"
