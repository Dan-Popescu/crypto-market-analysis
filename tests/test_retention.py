import pytest

from crypto_market_analysis.models import Observation
from crypto_market_analysis.retention import compact_liquidity
from crypto_market_analysis.storage import Store


def obs(t, value, metric="depth_bid_1pct", quote="USD", mode="live", covered=True, observed=None):
    return Observation(
        "x",
        "BTC",
        metric,
        t,
        observed if observed is not None else t,
        value,
        quote,
        quote,
        {"coverage_complete": covered, "band_percent": 1},
        mode,
    )


def test_compact_median_dimensions_versions_and_idempotence(tmp_path):
    store = Store(tmp_path / "retention.sqlite")
    now = 20 * 86400
    t = 10 * 86400
    store.put_observations(
        [
            obs(t + 300, 1),
            obs(t + 300, 2, observed=t + 400),
            obs(t + 600, 8, covered=False),
            obs(t + 900, 4),
            obs(t + 300, 40, quote="USDT"),
            obs(t + 300, 30, mode="demo"),
            obs(t + 300, 12, metric="funding_rate_actual"),
            obs(now - 300, 99),
        ]
    )
    result = compact_liquidity(store, now=now)
    assert result == {"aggregates_written": 3, "raw_rows_deleted": 6, "remaining_old_rows": 0}
    live = store.observations(as_of=now, days=30)
    aggregate = next(
        o for o in live if o.metric == "depth_bid_1pct" and o.quote == "USD" and o.event_time == t + 3600
    )
    assert aggregate.value == 4 and aggregate.observed_at == now
    assert aggregate.metadata["min"] == 2 and aggregate.metadata["max"] == 8
    assert aggregate.metadata["sample_count"] == 3
    assert aggregate.metadata["coverage_fraction"] == pytest.approx(2 / 3)
    assert not aggregate.metadata["coverage_complete"]
    assert any(o.metric == "funding_rate_actual" for o in live)
    assert any(o.quote == "USDT" and o.value == 40 for o in live)
    assert store.observations(as_of=now, mode="demo", days=30)[0].value == 30
    assert compact_liquidity(store, now=now)["raw_rows_deleted"] == 0


def test_partial_hour_retained_and_day_batches(tmp_path):
    store = Store(tmp_path / "retention.sqlite")
    now = 20 * 86400 + 1800
    cutoff = now - 7 * 86400
    store.put_observations([obs(10 * 86400 + 1, 1), obs(11 * 86400 + 1, 2), obs(cutoff - 60, 3)])
    first = compact_liquidity(store, now=now)
    assert first["raw_rows_deleted"] == 1 and first["remaining_old_rows"] == 1
    assert compact_liquidity(store, now=now)["raw_rows_deleted"] == 1
    assert compact_liquidity(store, now=now)["raw_rows_deleted"] == 0
    assert any(o.value == 3 for o in store.observations(as_of=now, days=30))


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_invalid_keep_days(tmp_path, invalid):
    with pytest.raises(ValueError):
        compact_liquidity(Store(tmp_path / "retention.sqlite"), keep_days=invalid, now=20 * 86400)


def test_failed_aggregate_write_does_not_remove_raw(tmp_path, monkeypatch):
    store = Store(tmp_path / "retention.sqlite")
    t = 10 * 86400
    store.put_observations([obs(t + 10, 7)])

    def fail(_):
        raise RuntimeError("write failed")

    monkeypatch.setattr(store, "put_observations", fail)
    with pytest.raises(RuntimeError):
        compact_liquidity(store, now=20 * 86400)
    assert store.counts()["observations"] == 1


def test_hour_boundary_raw_does_not_collide_with_previous_aggregate(tmp_path):
    store = Store(tmp_path / "retention.sqlite")
    t = 10 * 86400
    store.put_observations([obs(t + 10, 2), obs(t + 3600, 4)])
    compact_liquidity(store, now=20 * 86400)
    result = store.observations(as_of=20 * 86400, days=30)
    assert [(o.event_time, o.value) for o in reversed(result)] == [(t + 3600, 2), (t + 7200, 4)]
