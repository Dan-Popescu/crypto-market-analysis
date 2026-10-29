"""Bounded synthetic repairs with injected transports; no provider access."""
import json
from email.utils import formatdate

import pytest

from crypto_market_analysis.automatic_repair import (
    AutoRepairRequest,
    automatic_repair_status,
    plan_auto_repairs,
    run_auto_repairs,
)
from crypto_market_analysis.history_dataset import (
    DatasetConfig,
    HistoryDataset,
    RESTCapture,
    UnsupportedMarket,
)

HOUR = 3600
NOW = 1000 * HOUR


def dataset(tmp_path, *, start=995, end=1000, clock=lambda: NOW, **bounds):
    return HistoryDataset(DatasetConfig("synthetic-repair", str(tmp_path / "research"),
                                        start * HOUR, end * HOUR, **bounds), clock=clock)


def row(hour):
    return [hour * HOUR * 1000, 10, 12, 9, 11, 2]


def request(**overrides):
    return AutoRepairRequest(**{"markets": (("BTC", "EUR"),), **overrides})


def all_rows(_asset, _quote, since, limit):
    return RESTCapture([row(since // HOUR + n) for n in range(limit)], raw=b"original synthetic response")


def checkpoint(ds):
    return automatic_repair_status(ds.store.path)["tasks"][0]


def test_missing_inspection_noninitializing_and_mode_separation(tmp_path):
    missing = tmp_path / "absent" / "observer.sqlite"
    assert plan_auto_repairs(missing, now=NOW)["status"] == "not_configured"
    assert run_auto_repairs(missing, clock=lambda: NOW)["read_only"]
    assert automatic_repair_status(missing)["status"] == "not_configured"
    assert not missing.parent.exists()
    ds = dataset(tmp_path)
    before = {p.name: p.read_bytes() for p in ds.directory.iterdir() if p.is_file()}
    assert plan_auto_repairs(ds.store.path, now=NOW)["requests_planned"] == 4
    assert run_auto_repairs(ds.store.path, mode="live", apply=True, fetch=lambda *_: pytest.fail(),
                            clock=lambda: NOW)["status"] == "selection_mismatch"
    assert automatic_repair_status(ds.store.path)["status"] == "not_recorded"
    # SQLite may create transient WAL/SHM locks for a mode=ro reader.
    assert before == {p.name: p.read_bytes() for p in ds.directory.iterdir() if p.name in before}


def test_retention_closure_exact_counts_and_bounded_plan(tmp_path):
    ds = dataset(tmp_path, start=0, end=1002)
    plan = plan_auto_repairs(ds.store.path, request(max_hours=3, max_ranges=1), now=NOW)
    market = plan["markets"][0]
    assert market["remaining_hours"] == 1002
    assert market["requires_archives_hours"] == 281
    assert market["recoverable_hours"] == 719
    assert market["not_closed_hours"] == 2
    assert market["gaps"] == [{"start": 0, "end": 281 * HOUR, "status": "requires_archives"}]
    assert market["gap_ranges_omitted"] == 2
    assert plan["hours_planned"] == 3
    assert plan["tasks"][0]["request_start"] == 281 * HOUR
    seen = []
    run_auto_repairs(ds.store.path, request(max_hours=3), apply=True,
                     fetch=lambda a, q, s, limit: seen.append((s, limit)) or all_rows(a, q, s, limit),
                     clock=lambda: NOW)
    assert seen == [(281 * HOUR, 3)]
    old = dataset(tmp_path / "old", start=0, end=3)
    result = run_auto_repairs(old.store.path, apply=True, fetch=lambda *_: pytest.fail("archive gap queried"),
                              clock=lambda: NOW)
    assert result["requests_attempted"] == 0
    assert all(m["requires_archives_hours"] == 3 for m in result["markets"])


def test_default_dry_run_dict_validation_and_resource_bounds(tmp_path):
    ds = dataset(tmp_path, max_requests=2)
    request_json = {"markets": [["BTC", "EUR"]], "request_budget": 1, "max_hours": 2}
    result = run_auto_repairs(ds.store.path, request_json, fetch=lambda *_: pytest.fail(), clock=lambda: NOW)
    assert result["read_only"] and result["hours_planned"] == 2
    assert automatic_repair_status(ds.store.path)["status"] == "not_recorded"
    for bad in ({"request_budget": True}, {"markets": [["BTC", "USDT"]]}, {"max_hours": 2881},
                {"unknown": 1}, {"markets": None}, []):
        with pytest.raises(ValueError):
            plan_auto_repairs(ds.store.path, bad, now=NOW)
    with pytest.raises(ValueError, match="dataset request bound"):
        plan_auto_repairs(ds.store.path, request(request_budget=3), now=NOW)
    with pytest.raises(ValueError, match="fetcher"):
        run_auto_repairs(ds.store.path, request(request_budget=1), apply=True, clock=lambda: NOW)


def test_partial_response_restart_exact_remaining_and_lineage(tmp_path):
    clock = [NOW]
    ds = dataset(tmp_path, clock=lambda: clock[0])
    result = run_auto_repairs(ds.store.path, request(), apply=True,
                              fetch=lambda *_: RESTCapture([row(995), row(999)], raw=b"partial"),
                              clock=lambda: clock[0])
    assert result["requests_attempted"] == 1
    assert result["markets"][0]["remaining_hours"] == 3
    assert checkpoint(ds)["remaining_hours"] == 3
    assert checkpoint(ds)["status"] == "pending"
    assert not plan_auto_repairs(ds.store.path, request(), now=NOW)["tasks"]
    reopened = HistoryDataset(ds.config, clock=lambda: clock[0])
    clock[0] += 60
    result = run_auto_repairs(reopened.store.path, request(), apply=True, fetch=all_rows, clock=lambda: clock[0])
    assert result["markets"][0]["remaining_hours"] == 0
    assert checkpoint(ds)["status"] == "complete"
    assert checkpoint(ds)["requests"] == 2
    candles = reopened.store.candles("BTC", quote="EUR", mode="import", as_of=clock[0])
    assert len(candles) == 5
    assert [c.observed_at for c in candles] == [NOW, NOW + 60, NOW + 60, NOW + 60, NOW]
    assert reopened.store.candles("BTC", quote="EUR", mode="live", as_of=clock[0]) == []
    capture = reopened.manifest()["captures"][-1]
    assert capture["captured_at"] == clock[0] and capture["historical_known_at"] == "unknown"
    assert capture["original_mode"] == "public_rest_capture" and capture["data_mode"] == "import"
    assert capture["raw_available"]


def test_empty_unsupported_and_market_failure_are_independent(tmp_path):
    ds = dataset(tmp_path, start=999)
    requested = []

    def fetch(asset, quote, since, limit):
        requested.append((asset, quote))
        if (asset, quote) == ("BTC", "EUR"):
            raise UnsupportedMarket("synthetic missing pair")
        if (asset, quote) == ("ETH", "EUR"):
            return RESTCapture([])
        if (asset, quote) == ("BTC", "USD"):
            raise RuntimeError("synthetic failed source")
        return all_rows(asset, quote, since, limit)

    result = run_auto_repairs(ds.store.path, apply=True, fetch=fetch, clock=lambda: NOW)
    assert requested == [("BTC", "EUR"), ("ETH", "EUR"), ("BTC", "USD"), ("ETH", "USD")]
    assert [o["status"] for o in result["outcomes"]] == ["unsupported", "unknown_gap", "error", "complete"]
    assert [m["remaining_hours"] for m in result["markets"]] == [1, 1, 1, 0]
    planned = plan_auto_repairs(ds.store.path, now=NOW + 60)
    assert [(t["asset"], t["quote"]) for t in planned["tasks"]] == [("ETH", "EUR"), ("BTC", "USD")]
    assert automatic_repair_status(ds.store.path, limit=2)["tasks_omitted"] == 2


@pytest.mark.parametrize("value", ["180", formatdate(NOW + 180, usegmt=True)])
def test_retry_after_deadline_persists_without_sleeping(tmp_path, value):
    ds = dataset(tmp_path)

    class RateLimited(Exception):
        headers = {"Retry-After": value}

    def fetch(*_):
        raise RateLimited("synthetic")

    run_auto_repairs(ds.store.path, request(), apply=True, fetch=fetch, clock=lambda: NOW)
    assert checkpoint(ds)["retry_at"] == NOW + 180
    assert not plan_auto_repairs(ds.store.path, request(), now=NOW + 179)["tasks"]
    assert plan_auto_repairs(ds.store.path, request(), now=NOW + 180)["tasks"]


def test_midcycle_interruption_checkpoint_and_resume(tmp_path):
    ds = dataset(tmp_path, start=999)
    seen = []

    def interrupted(asset, quote, since, limit):
        seen.append((asset, quote))
        if len(seen) == 2:
            raise KeyboardInterrupt("synthetic process interruption")
        return all_rows(asset, quote, since, limit)

    with pytest.raises(KeyboardInterrupt):
        run_auto_repairs(ds.store.path, apply=True, fetch=interrupted, clock=lambda: NOW)
    tasks = automatic_repair_status(ds.store.path)["tasks"]
    assert {t["status"] for t in tasks} == {"complete", "inflight"}
    assert next(t for t in tasks if t["status"] == "inflight")["requests"] == 1
    assert seen == [("BTC", "EUR"), ("ETH", "EUR")]
    replayed = []
    result = run_auto_repairs(ds.store.path, apply=True,
                              fetch=lambda a, q, s, n: replayed.append((a, q)) or all_rows(a, q, s, n),
                              clock=lambda: NOW + 60)
    assert replayed == [("ETH", "EUR"), ("BTC", "USD"), ("ETH", "USD")]
    assert all(m["remaining_hours"] == 0 for m in result["markets"])
    assert next(t for t in automatic_repair_status(ds.store.path)["tasks"] if t["asset"] == "ETH" and t["quote"] == "EUR")["requests"] == 2


def test_capture_materialized_before_checkpoint_fault_is_reconciled(tmp_path, monkeypatch):
    ds = dataset(tmp_path, start=999)
    original = HistoryDataset.append_rest

    def fault(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise KeyboardInterrupt("synthetic post-materialization crash")

    monkeypatch.setattr(HistoryDataset, "append_rest", fault)
    with pytest.raises(KeyboardInterrupt):
        run_auto_repairs(ds.store.path, request(), apply=True, fetch=all_rows, clock=lambda: NOW)
    assert checkpoint(ds)["status"] == "inflight"
    monkeypatch.setattr(HistoryDataset, "append_rest", original)
    result = run_auto_repairs(ds.store.path, request(), apply=True,
                              fetch=lambda *_: pytest.fail("already captured"), clock=lambda: NOW)
    assert result["requests_attempted"] == 0
    assert checkpoint(ds)["status"] == "complete" and checkpoint(ds)["remaining_hours"] == 0


def test_manual_plan_is_never_overwritten(tmp_path):
    ds = dataset(tmp_path)
    manual = ds.plan_repair("BTC", "EUR", 996 * HOUR, 998 * HOUR)
    result = run_auto_repairs(ds.store.path, request(), apply=True, fetch=lambda *_: pytest.fail(),
                              clock=lambda: NOW)
    assert result["requests_attempted"] == 0
    assert result["markets"][0]["manual_repair_active"]
    with ds.store.connection() as con:
        assert json.loads(con.execute("SELECT payload FROM history_repairs").fetchone()[0]) == manual


def test_out_of_range_rows_never_fill_other_gaps_or_future(tmp_path):
    ds = dataset(tmp_path, start=995, end=1002)
    result = run_auto_repairs(ds.store.path, request(max_hours=2), apply=True,
                              fetch=lambda *_: RESTCapture([row(994), row(1001)], raw=b"out of range"),
                              clock=lambda: NOW)
    assert result["markets"][0]["remaining_hours"] == 7
    assert result["outcomes"][0]["status"] == "unknown_gap"
    assert ds.store.candles("BTC", quote="EUR", mode="import", as_of=NOW + 3 * HOUR) == []
    capture = ds.manifest()["captures"][0]
    assert capture["rows"] == 0 and capture["until"] == 997 * HOUR
    assert capture["raw_available"]


def test_global_hour_and_request_budgets_survive_replanning(tmp_path):
    ds = dataset(tmp_path, start=998)
    seen = []
    result = run_auto_repairs(ds.store.path, {"max_hours": 3, "request_budget": 2}, apply=True,
                              fetch=lambda a, q, s, n: seen.append((a, q, s, n)) or all_rows(a, q, s, n),
                              clock=lambda: NOW)
    assert result["requests_attempted"] == 2
    assert sum(call[3] for call in seen) == 3
    assert [m["remaining_hours"] for m in result["markets"]] == [0, 1, 2, 2]


def test_moving_retention_rechecks_before_each_request(tmp_path):
    clock = [NOW]
    ds = dataset(tmp_path, start=281, end=283)
    seen = []

    def fetch(asset, quote, since, limit):
        seen.append((asset, quote, since, limit))
        clock[0] += 2 * HOUR
        return all_rows(asset, quote, since, limit)

    result = run_auto_repairs(ds.store.path, apply=True, fetch=fetch, clock=lambda: clock[0])
    assert len(seen) == 1
    assert all(m["requires_archives_hours"] == 2 for m in result["markets"][1:])


def test_pending_journal_inspection_never_replays_and_apply_preserves_original_receipt(tmp_path, monkeypatch):
    from crypto_market_analysis.storage import Store

    ds = dataset(tmp_path, start=999)
    original = Store.put_candles

    def fault(*_):
        raise KeyboardInterrupt("synthetic crash with immutable pending journal")

    monkeypatch.setattr(Store, "put_candles", fault)
    with pytest.raises(KeyboardInterrupt):
        run_auto_repairs(ds.store.path, request(), apply=True, fetch=all_rows, clock=lambda: NOW)
    journal = next(ds.directory.glob("pending-*.json"))
    payload = journal.read_bytes()
    assert plan_auto_repairs(ds.store.path, request(), now=NOW)["markets"][0]["remaining_hours"] == 1
    assert automatic_repair_status(ds.store.path)["tasks"][0]["status"] == "inflight"
    assert journal.read_bytes() == payload
    monkeypatch.setattr(Store, "put_candles", original)
    result = run_auto_repairs(ds.store.path, request(), apply=True, fetch=lambda *_: pytest.fail(),
                              clock=lambda: NOW + 10)
    assert result["requests_attempted"] == 0 and not journal.exists()
    assert checkpoint(ds)["status"] == "complete"
    assert ds.store.candles("BTC", quote="EUR", mode="import", as_of=NOW + 10)[0].observed_at == NOW


def test_existing_gap_that_ages_out_becomes_archives_only(tmp_path):
    ds = dataset(tmp_path, start=999)
    run_auto_repairs(ds.store.path, request(), apply=True, fetch=lambda *_: RESTCapture([]), clock=lambda: NOW)
    result = run_auto_repairs(ds.store.path, request(), apply=True, fetch=lambda *_: pytest.fail(),
                              clock=lambda: NOW + 720 * HOUR)
    assert result["requests_attempted"] == 0
    assert checkpoint(ds)["status"] == "requires_archives"
    assert checkpoint(ds)["remaining_hours"] == 1
    assert result["markets"][0]["requires_archives_hours"] == 1


def test_many_ranges_in_one_market_do_not_starve_other_native_markets(tmp_path):
    ds = dataset(tmp_path, start=993)
    ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([row(994), row(996), row(998)]),
                   since=993 * HOUR)
    plan = plan_auto_repairs(ds.store.path, now=NOW)
    assert [(t["asset"], t["quote"]) for t in plan["tasks"]] == list(AutoRepairRequest().markets)


def test_repeated_transport_failures_back_off_exponentially(tmp_path):
    ds = dataset(tmp_path)

    def failed(*_):
        raise RuntimeError("synthetic repeated failure")

    run_auto_repairs(ds.store.path, request(), apply=True, fetch=failed, clock=lambda: NOW)
    run_auto_repairs(ds.store.path, request(), apply=True, fetch=failed, clock=lambda: NOW + 60)
    assert checkpoint(ds)["retry_at"] == NOW + 180
    assert checkpoint(ds)["consecutive_failures"] == 2
    assert not plan_auto_repairs(ds.store.path, request(), now=NOW + 179)["tasks"]


@pytest.mark.parametrize("catalogue_failure", [False, True])
def test_kraken_adapter_preserves_retry_after_from_client(catalogue_failure):
    from ccxt import RateLimitExceeded

    from crypto_market_analysis.history_dataset import KrakenRESTCapture

    class Client:
        last_response_headers = {"retry-after": "300"}

        def load_markets(self):
            if catalogue_failure:
                raise RateLimitExceeded("synthetic catalogue limit")
            return {"BTC/EUR": {"spot": True}}

        def fetch_ohlcv(self, *_args, **_kwargs):
            raise RateLimitExceeded("synthetic OHLC limit")

    with pytest.raises(RateLimitExceeded) as failure:
        KrakenRESTCapture(Client())("BTC", "EUR", 999 * HOUR, 1)
    assert failure.value.retry_after == "300"
    assert failure.value.retry_after_basis == "ccxt_client_last_response_header_unverified"


def test_small_hour_budget_repairs_all_four_long_native_gaps_and_resumes(tmp_path):
    ds = dataset(tmp_path, start=281, end=1000)
    bounds = {"max_hours": 24, "request_budget": 4}
    plan = plan_auto_repairs(ds.store.path, bounds, now=NOW)
    assert plan["requests_planned"] == 4 and plan["hours_planned"] == 24
    assert [(t["asset"], t["quote"]) for t in plan["tasks"]] == list(AutoRepairRequest().markets)
    assert [t["request_hours"] for t in plan["tasks"]] == [6, 6, 6, 6]
    assert all(t["request_start"] == 281 * HOUR and t["request_end"] == 287 * HOUR for t in plan["tasks"])
    calls = []
    result = run_auto_repairs(ds.store.path, bounds, apply=True,
                              fetch=lambda a, q, s, n: calls.append((a, q, s, n)) or all_rows(a, q, s, n),
                              clock=lambda: NOW)
    assert result["requests_attempted"] == 4
    assert sum(call[3] for call in calls) == 24
    assert [m["remaining_hours"] for m in result["markets"]] == [713, 713, 713, 713]
    assert all(t["status"] == "pending" and t["remaining_hours"] == 713
               for t in automatic_repair_status(ds.store.path)["tasks"])
    reopened = HistoryDataset(ds.config, clock=lambda: NOW + 60)
    resumed = run_auto_repairs(reopened.store.path, bounds, apply=True,
                               fetch=lambda a, q, s, n: calls.append((a, q, s, n)) or all_rows(a, q, s, n),
                               clock=lambda: NOW + 60)
    assert resumed["requests_attempted"] == 4
    assert [m["remaining_hours"] for m in resumed["markets"]] == [707, 707, 707, 707]
    assert all(call[2] == 287 * HOUR and call[3] == 6 for call in calls[4:])
    assert all(call[2] >= 281 * HOUR for call in calls)


def test_unused_short_gap_share_is_redistributed_without_exceeding_bounds(tmp_path):
    ds = dataset(tmp_path, start=900, end=1000)
    ds.append_rest("ETH", "USD", lambda *_: RESTCapture([row(h) for h in range(901, 1000)]),
                   since=901 * HOUR)
    plan = plan_auto_repairs(ds.store.path, {"max_hours": 24, "request_budget": 4}, now=NOW)
    assert [t["request_hours"] for t in plan["tasks"]] == [8, 8, 7, 1]
    assert plan["requests_planned"] == 4 and plan["hours_planned"] == 24


@pytest.mark.parametrize("bounds,expected", [({"max_ranges": 2}, [12, 12]),
                                            ({"request_budget": 2}, [12, 12]),
                                            ({"max_hours": 2}, [1, 1])])
def test_fair_hour_allocation_respects_range_request_and_small_hour_caps(tmp_path, bounds, expected):
    ds = dataset(tmp_path, start=900, end=1000)
    plan = plan_auto_repairs(ds.store.path, {"max_hours": 24, **bounds}, now=NOW)
    assert [t["request_hours"] for t in plan["tasks"]] == expected
    assert plan["hours_planned"] == sum(expected)


def test_hour_shares_exclude_manual_and_retry_blocked_markets(tmp_path):
    ds = dataset(tmp_path, start=900, end=1000)
    ds.plan_repair("BTC", "EUR", 900 * HOUR, 1000 * HOUR)
    run_auto_repairs(ds.store.path, {"markets": [["ETH", "EUR"]]}, apply=True,
                     fetch=lambda *_: RESTCapture([]), clock=lambda: NOW)
    plan = plan_auto_repairs(ds.store.path, {"max_hours": 24, "request_budget": 4}, now=NOW)
    assert [(t["asset"], t["quote"], t["request_hours"]) for t in plan["tasks"]] == [
        ("BTC", "USD", 12), ("ETH", "USD", 12)]
    assert plan["hours_planned"] == 24 and plan["requests_planned"] == 2


def test_archived_hole_does_not_hide_empty_response_backoff_across_restarts(tmp_path):
    ds = dataset(tmp_path, start=281, end=1000)
    bounds = request(max_hours=6, request_budget=1)
    first = run_auto_repairs(ds.store.path, bounds, apply=True, fetch=all_rows, clock=lambda: NOW)
    assert first["outcomes"][0]["status"] == "pending"
    assert first["outcomes"][0]["remaining_hours"] == 713
    assert first["outcomes"][0]["consecutive_failures"] == 0
    aged_now = NOW + 7 * HOUR
    for attempt, (offset, delay) in enumerate(((0, 60), (60, 120)), start=1):
        reopened = HistoryDataset(ds.config, clock=lambda offset=offset: aged_now + offset)
        result = run_auto_repairs(reopened.store.path, bounds, apply=True, fetch=lambda *_: RESTCapture([]),
                                  clock=lambda offset=offset: aged_now + offset)
        state = result["outcomes"][0]
        assert state["status"] == "unknown_gap"
        assert state["consecutive_failures"] == attempt
        assert state["retry_at"] == aged_now + offset + delay
        assert state["remaining_hours"] == 713
        assert result["markets"][0]["requires_archives_hours"] == 1
        assert result["markets"][0]["recoverable_hours"] == 712
    assert not plan_auto_repairs(ds.store.path, bounds, now=aged_now + 179)["tasks"]
    resumed = run_auto_repairs(ds.store.path, bounds, apply=True,
                               fetch=lambda _a, _q, since, _n: RESTCapture([row(since // HOUR)]),
                               clock=lambda: aged_now + 180)
    state = resumed["outcomes"][0]
    assert state["status"] == "pending" and state["consecutive_failures"] == 0
    assert state["remaining_hours"] == 712 and state["retry_at"] == aged_now + 240
    assert resumed["markets"][0]["requires_archives_hours"] == 1
    assert resumed["markets"][0]["recoverable_hours"] == 711


def test_retention_advancing_during_empty_fetch_is_not_materialization_progress(tmp_path):
    ds = dataset(tmp_path, start=281, end=1000)
    bounds = request(max_hours=6, request_budget=1)
    clock = [NOW]

    def advancing_empty(*_):
        clock[0] += HOUR
        return RESTCapture([])

    first = run_auto_repairs(ds.store.path, bounds, apply=True, fetch=advancing_empty,
                             clock=lambda: clock[0])
    assert first["outcomes"][0]["status"] == "unknown_gap"
    assert first["outcomes"][0]["consecutive_failures"] == 1
    assert first["markets"][0]["requires_archives_hours"] == 1
    clock[0] += 60
    second = run_auto_repairs(ds.store.path, bounds, apply=True, fetch=advancing_empty,
                              clock=lambda: clock[0])
    assert second["outcomes"][0]["status"] == "unknown_gap"
    assert second["outcomes"][0]["consecutive_failures"] == 2
    assert second["outcomes"][0]["retry_at"] == clock[0] + 120
    assert second["markets"][0]["requires_archives_hours"] == 2
    assert second["markets"][0]["remaining_hours"] == 719


def test_interrupted_request_checkpoint_counts_archived_and_recent_holes(tmp_path):
    ds = dataset(tmp_path, start=281, end=1000)
    bounds = request(max_hours=6, request_budget=1)
    run_auto_repairs(ds.store.path, bounds, apply=True, fetch=all_rows, clock=lambda: NOW)

    def interrupted(*_):
        raise KeyboardInterrupt("synthetic interruption with one archived missing hour")

    with pytest.raises(KeyboardInterrupt):
        run_auto_repairs(ds.store.path, bounds, apply=True, fetch=interrupted, clock=lambda: NOW + 7 * HOUR)
    state = checkpoint(ds)
    assert state["status"] == "inflight" and state["remaining_hours"] == 713
    plan = plan_auto_repairs(ds.store.path, bounds, now=NOW + 7 * HOUR)
    assert plan["markets"][0]["requires_archives_hours"] == 1
    assert plan["markets"][0]["recoverable_hours"] == 712
