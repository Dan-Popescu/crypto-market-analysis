"""Finalized liquidity hours and source-native funding remain honest at public boundaries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest

from crypto_market_analysis.models import Candle, Observation
from crypto_market_analysis.retention import compact_liquidity
from crypto_market_analysis.storage import Store
from crypto_market_analysis.vigilance import assess_vigilance

DAY = 86400
HOUR = 10 * DAY
NOW = 20 * DAY


def raw(event, value, observed=None, **changes):
    observation = Observation("venue", "BTC", "depth_bid_1pct", event,
                              event if observed is None else observed, value, "EUR", "EUR",
                              {"symbol": "BTC/EUR", "band_percent": 1, "coverage_complete": True})
    return replace(observation, **changes)


def hourly(store, as_of=NOW, mode="live"):
    return [row for row in store.observations(as_of=as_of, days=30, mode=mode)
            if row.metadata.get("aggregation") == "hourly_median"]


def prices(now=NOW):
    return [Candle("venue", "BTC", "EUR", "1h", now - 7200, now - 3600,
                   100, 100, 100, 100, 1, now - 3600),
            Candle("venue", "BTC", "EUR", "1h", now - 3600, now,
                   100, 100, 100, 100, 1, now)]


def fail_deletions(store, monkeypatch):
    connection = store.connection

    class InterruptedDeletion:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM observations"):
                raise RuntimeError("interrupted raw deletion")
            return self.con.execute(sql, *args)

    @contextmanager
    def interrupted():
        with connection() as con:
            yield InterruptedDeletion(con)

    monkeypatch.setattr(store, "connection", interrupted)
    return connection


def test_late_batch_preserves_hand_median_and_is_explicitly_unmerged(tmp_path):
    store = Store(tmp_path / "retention.sqlite")
    store.put_observations([raw(HOUR + 100, 1), raw(HOUR + 200, 3)])
    compact_liquidity(store, now=NOW)
    before = hourly(store)[0]
    assert (before.value, before.metadata["sample_count"]) == (2, 2)
    store.put_observations([raw(HOUR + 300, 100, NOW + 1)])
    result = compact_liquidity(store, now=NOW + 2)
    preserved = hourly(store, NOW + 2)[0]
    assert (preserved.value, preserved.metadata["sample_count"]) == (2, 2)
    assert preserved.metadata["retention_status"] == "partial_unmerged"
    assert preserved.metadata["coverage_complete"] is False
    assert hourly(store, NOW)[0] == before
    late = next(row for row in store.observations(as_of=NOW + 2, days=30)
                if row.metadata.get("aggregation") == "late_raw_unmerged")
    assert late.value == 100 and late.observed_at == NOW + 1
    assert late.metadata["retention_status"] == "partial_unmerged"
    assert late.metadata["coverage_complete"] is False
    assert late.metadata["original_coverage_complete"] is True
    vigilance = assess_vigilance(prices(), store.observations(as_of=NOW + 2, days=30), NOW + 2)
    assert vigilance["liquidity"]["depth_bid_1pct"]["status"] == "missing"
    assert vigilance["evidence"]["observations"] == []
    assert result["late_rows_quarantined"] == 1
    assert result["remaining_old_rows"] == 0
    count = store.counts()["observations"]
    assert compact_liquidity(store, now=NOW + 3)["raw_rows_deleted"] == 0
    assert store.counts()["observations"] == count
    # A quarantined old hour must not consume every future maintenance day budget.
    store.put_observations([raw(HOUR + DAY + 1, 20)])
    assert compact_liquidity(store, now=NOW + 4)["raw_rows_deleted"] == 1
    assert len(hourly(store, NOW + 4)) == 2


def test_late_versions_keep_native_dimensions_and_availability(tmp_path):
    store = Store(tmp_path / "retention.sqlite")
    base = raw(HOUR + 100, 1)
    identities = [base, replace(base, source="other"), replace(base, quote="USD", unit="USD"),
                  replace(base, data_mode="import"),
                  replace(base, metadata={**base.metadata, "band_percent": 2})]
    store.put_observations(identities)
    compact_liquidity(store, now=NOW)
    late = raw(HOUR + 200, 100, NOW + 1)
    revision = replace(late, value=101, observed_at=NOW + 2)
    store.put_observations([late, revision])
    compact_liquidity(store, now=NOW + 3)
    for cutoff, expected in ((NOW + 1, 100), (NOW + 2, 101)):
        row = next(row for row in store.observations(as_of=cutoff, days=30)
                   if row.metadata.get("aggregation") == "late_raw_unmerged")
        assert row.value == expected and row.observed_at == cutoff
        assert (row.source, row.asset, row.quote, row.unit, row.data_mode) == ("venue", "BTC", "EUR", "EUR", "live")
        assert row.metadata["symbol"] == "BTC/EUR"
    marked = [row for row in hourly(store, NOW + 3)
              if row.metadata.get("retention_status") == "partial_unmerged"]
    assert len(marked) == 1 and marked[0].metadata["band_percent"] == 1
    assert hourly(store, NOW + 3, "import")[0].metadata["coverage_complete"] is True


def test_interrupted_initial_delete_retry_does_not_quarantine_covered_samples(tmp_path, monkeypatch):
    store = Store(tmp_path / "retention.sqlite")
    store.put_observations([raw(HOUR + 100, 1), raw(HOUR + 200, 3)])
    connection = fail_deletions(store, monkeypatch)
    with pytest.raises(RuntimeError, match="interrupted raw deletion"):
        compact_liquidity(store, now=NOW)
    monkeypatch.setattr(store, "connection", connection)
    assert store.counts()["observations"] == 3
    result = compact_liquidity(store, now=NOW + 1)
    assert result == {"aggregates_written": 0, "raw_rows_deleted": 2, "remaining_old_rows": 0}
    assert hourly(store, NOW + 1)[0].metadata["coverage_complete"] is True
    assert store.counts()["observations"] == 1


def test_interrupted_late_delete_retry_does_not_duplicate_older_versions(tmp_path, monkeypatch):
    store = Store(tmp_path / "retention.sqlite")
    store.put_observations([raw(HOUR + 100, 1), raw(HOUR + 200, 3)])
    compact_liquidity(store, now=NOW)
    store.put_observations([raw(HOUR + 300, 100, NOW + 1), raw(HOUR + 300, 101, NOW + 2)])
    connection = fail_deletions(store, monkeypatch)
    with pytest.raises(RuntimeError, match="interrupted raw deletion"):
        compact_liquidity(store, now=NOW + 3)
    monkeypatch.setattr(store, "connection", connection)
    before = store.counts()["observations"]
    result = compact_liquidity(store, now=NOW + 4)
    assert result["aggregates_written"] == 0
    assert result["raw_rows_deleted"] == 2
    assert store.counts()["observations"] == before - 2
    assert hourly(store, NOW + 4)[0].metadata["sample_count"] == 2


def test_concurrent_insert_survives_captured_deletion_then_is_quarantined(tmp_path, monkeypatch):
    store = Store(tmp_path / "retention.sqlite")
    store.put_observations([raw(HOUR + 100, 1), raw(HOUR + 200, 3)])
    write = store.put_observations

    def concurrent(rows):
        # Collector writes after the read snapshot but before aggregate insertion.
        write([raw(HOUR + 300, 100, NOW)])
        return write(rows)

    monkeypatch.setattr(store, "put_observations", concurrent)
    first = compact_liquidity(store, now=NOW)
    monkeypatch.setattr(store, "put_observations", write)
    assert first["raw_rows_deleted"] == 2 and first["remaining_old_rows"] == 1
    second = compact_liquidity(store, now=NOW + 1)
    assert second["late_rows_quarantined"] == 1 and second["remaining_old_rows"] == 0
    assert (hourly(store, NOW + 1)[0].value, hourly(store, NOW + 1)[0].metadata["sample_count"]) == (2, 2)


def test_preexisting_future_version_is_not_mistaken_for_covered_raw_id(tmp_path):
    store = Store(tmp_path / "retention.sqlite")
    # The future-availability row has a lower ID than the snapshot watermark.
    store.put_observations([raw(HOUR + 300, 100, NOW + 1), raw(HOUR + 100, 1), raw(HOUR + 200, 3)])
    compact_liquidity(store, now=NOW)
    assert hourly(store)[0].value == 2
    result = compact_liquidity(store, now=NOW + 2)
    assert result["late_rows_quarantined"] == 1
    assert hourly(store, NOW + 2)[0].metadata["sample_count"] == 2


def test_competing_compactors_serialize_while_collector_can_insert(tmp_path, monkeypatch):
    path = tmp_path / "retention.sqlite"
    first, second, collector = Store(path), Store(path), Store(path)
    collector.put_observations([raw(HOUR + 100, 1), raw(HOUR + 200, 3)])
    writing, release = Event(), Event()
    write = first.put_observations

    def paused(rows):
        writing.set()
        assert release.wait(5), "test failed to release the first compactor"
        return write(rows)

    monkeypatch.setattr(first, "put_observations", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(compact_liquidity, first, now=NOW)
        try:
            assert writing.wait(5), "first compactor did not reach its write"
            following = pool.submit(compact_liquidity, second, now=NOW)
            # This writes while the first compactor holds its sidecar lock.
            collector.put_observations([raw(HOUR + 300, 100, NOW)])
        finally:
            release.set()
        initial.result(timeout=5)
        assert following.result(timeout=5)["late_rows_quarantined"] == 1
    aggregate = hourly(collector)[0]
    assert (aggregate.value, aggregate.metadata["sample_count"]) == (2, 2)
    assert aggregate.metadata["retention_status"] == "partial_unmerged"


@pytest.mark.parametrize("metadata,status", [
    ({"funding_interval_seconds": 28800}, "ok"),
    ({"interval_seconds": 28800}, "ok"),
    ({"funding_interval_hours": 8}, "ok"),
    ({"funding_period_hours": 8}, "ok"),
    ({"funding_interval": "8h"}, "ok"),
    ({"funding_interval_seconds": 28800, "funding_period_hours": 8, "funding_interval": "480m"}, "ok"),
    ({"funding_interval_seconds": 28800, "funding_period_hours": 4}, "unknown_native_convention"),
    ({"funding_interval_hours": 8, "funding_interval": "1h"}, "unknown_native_convention"),
    ({"funding_interval": 8}, "unknown_native_convention"),
    ({"funding_interval_seconds": True}, "unknown_native_convention"),
])
def test_vigilance_uses_canonical_funding_period(metadata, status):
    funding = Observation("swap", "BTC", "funding_rate_actual", NOW, NOW, .001, "rate", "EUR",
                          {"symbol": "BTC/EUR:EUR", **metadata})
    result = assess_vigilance(prices(), [funding], NOW)
    assert result["derivatives"][0]["status"] == status
    assert result["derivatives"][0]["current"]["metadata"] == funding.metadata
