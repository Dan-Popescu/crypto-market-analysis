"""Hand-calculated market-context contracts, independent of aggregation helpers."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from crypto_market_analysis.context import market_context
from crypto_market_analysis.models import Candle


def history(days, start=0, constant=False):
    result = []
    for day in range(days):
        price = 100.0 if constant else 100.0 + day
        for hour in range(24):
            t = start + day * 86400 + hour * 3600
            result.append(
                Candle(
                    "reference",
                    "BTC",
                    "EUR",
                    "1h",
                    t,
                    t + 3600,
                    price,
                    price,
                    price,
                    price,
                    day + 1,
                    t + 3600,
                    data_mode="demo",
                )
            )
    return result


def test_two_year_return_requires_731_closes_and_exact_coverage():
    data = history(731)
    full = market_context(data, 731 * 86400)
    metric = full["lookbacks"]["730"]["return"]
    assert metric["value"] == pytest.approx(7.3)
    assert metric["coverage"]["required_closes"] == 731
    assert metric["coverage"]["obtained_closes"] == 731
    assert metric["unit"] == "fraction"
    assert metric["quote"] == "EUR" and metric["data_mode"] == "demo"
    assert metric["formula"] == "last_close / first_close - 1"
    short = market_context(data[24:], 731 * 86400)
    assert short["lookbacks"]["730"]["return"]["value"] is None
    assert short["lookbacks"]["730"]["return"]["status"] == "insufficient"


def test_gap_future_availability_and_named_high_range():
    data = history(10)
    data = [replace(c, high=150, low=50) if c.open_time // 86400 == 7 else c for c in data]
    result = market_context(data, 10 * 86400, [7])
    window = result["lookbacks"]["7"]
    assert window["decline_from_high"]["value"] == pytest.approx(109 / 150 - 1)
    assert window["range_position"]["value"] == pytest.approx((109 - 50) / 100)
    assert window["range_name"] == "trailing_7_complete_utc_days"
    assert window["latest_volume_ratio"]["value"] == pytest.approx(10 / 6.5)
    missing = [c for c in data if c.open_time != 5 * 86400 + 3600]
    assert market_context(missing, 10 * 86400, [7])["lookbacks"]["7"]["return"]["value"] is None
    future = replace(data[-1], close=999, high=999, observed_at=10 * 86400 + 1)
    assert market_context(data + [future], 10 * 86400, [7]) == result


def test_zero_range_and_zero_volume_do_not_invent_rank_or_ratio():
    data = [replace(c, volume=0) for c in history(9, constant=True)]
    window = market_context(data, 9 * 86400, [7])["lookbacks"]["7"]
    assert window["return"]["value"] == 0
    assert window["realized_volatility"]["value"] == 0
    assert window["range_position"]["status"] == "undefined"
    assert window["range_position"]["value"] is None
    assert window["latest_volume_ratio"]["value"] is None


def test_complete_utc_week_month_and_partial_latest_period():
    start = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    data = history(40, start=start)
    cutoff = start + 39 * 86400 + 12 * 3600
    result = market_context(data, cutoff, [7])
    month = result["calendar"]["monthly"]
    assert month["start"] == "2024-01-01T00:00:00+00:00"
    assert month["end"] == "2024-02-01T00:00:00+00:00"
    assert month["return"]["value"] == pytest.approx(0.30)
    assert month["coverage"]["required_days"] == 31
    week = result["calendar"]["weekly"]
    assert week["start"] == "2024-01-29T00:00:00+00:00"
    assert week["end"] == "2024-02-05T00:00:00+00:00"
    gap = [c for c in data if c.open_time != start + 31 * 86400]
    assert market_context(gap, cutoff, [7])["calendar"]["weekly"]["return"]["value"] is None


@pytest.mark.parametrize("bad", [True, float("nan"), 0, 3651])
def test_invalid_lookbacks(bad):
    with pytest.raises(ValueError):
        market_context([], 0, [bad])


def test_identity_argument_and_numeric_validation():
    data = history(2)
    with pytest.raises(ValueError):
        market_context(data + [replace(data[0], quote="USD")], 2 * 86400)
    with pytest.raises(ValueError):
        market_context(data, True)
    with pytest.raises(ValueError):
        market_context([replace(data[0], close=float("nan"))], 3600)
    assert market_context([], 0)["status"] == "unavailable"
