"""Issuance-relative event fixtures use explicit UTC hourly OHLC observations."""

from dataclasses import replace

import pytest

from crypto_market_analysis.models import Candle
from crypto_market_analysis.purchase_windows import analyze_windows, resolve_window


def hours(count=24, start=0, price=100):
    return [
        Candle(
            "reference",
            "BTC",
            "USD",
            "1h",
            start + i * 3600,
            start + (i + 1) * 3600,
            price,
            price,
            price,
            price,
            1,
            start + (i + 1) * 3600,
            data_mode="demo",
        )
        for i in range(count)
    ]


def test_post_issue_dip_recovery_and_calendar_down_are_distinct():
    data = hours()
    data[5] = replace(data[5], low=90)
    data[-1] = replace(data[-1], open=120, high=120, close=110)
    result = resolve_window(data, 100, 0, 0, 1, 0.05, 86400)
    assert result["endpoint_below_anchor"]["value"] is False
    assert result["below_anchor_touch"]["value"] is True
    crossing = result["below_anchor_touch"]["first_crossing_interval"]
    assert crossing["start"] == "1970-01-01T05:00:00+00:00"
    assert crossing["end"] == "1970-01-01T06:00:00+00:00"
    assert crossing["exact"] is False
    # Complete calendar day opens at 100 and closes at 110, despite a red final hour.
    assert result["calendar_down_day"]["days"][0]["value"] is False


def test_red_calendar_day_can_end_above_the_issuance_anchor():
    data = hours(price=120)
    data[-1] = replace(data[-1], low=110, close=110)
    result = resolve_window(data, 100, 0, 0, 1, 0.05, 86400)
    assert result["calendar_down_day"]["days"][0]["value"] is True
    assert result["endpoint_below_anchor"]["value"] is False


def test_preissuance_and_straddling_extremes_never_become_definite_events():
    data = hours(26)
    data[0] = replace(data[0], low=80)
    data[1] = replace(data[1], low=90)
    # First hour is entirely before issue; second hour straddles issuance.
    result = resolve_window(data, 100, 0, 5400, 1, 0.05, 100000)
    assert result["below_anchor_touch"]["state"] == "ambiguous"
    assert result["below_anchor_touch"]["value"] is None
    assert result["window_end"] == "1970-01-02T01:30:00+00:00"
    # A non-hour endpoint cannot be read as an exact completed-hour closing price.
    assert result["endpoint_below_anchor"]["state"] == "ambiguous"
    assert result["endpoint_below_anchor"]["price"] is None
    clean = [replace(c, low=100) if c.open_time == 3600 else c for c in data]
    assert resolve_window(clean, 100, 0, 5400, 1, 0.05, 100000)["below_anchor_touch"]["value"] is False


def test_pending_gaps_and_future_observations():
    data = hours()
    data[5] = replace(data[5], low=90, observed_at=90000)
    before = resolve_window(data, 100, 0, 0, 1, 0.05, 86400)
    assert before["below_anchor_touch"]["value"] is None
    assert before["below_anchor_touch"]["state"] == "ambiguous"
    after = resolve_window(data, 100, 0, 0, 1, 0.05, 90000)
    assert after["below_anchor_touch"]["value"] is True
    assert resolve_window(data, 100, 0, 0, 1, 0.05, 3600)["status"] == "pending"
    assert resolve_window(data, 100, 0, 0, 1, 0.05, 3600)["below_anchor_touch"]["state"] == "pending"
    gapped = resolve_window(data[1:], 100, 0, 0, 1, 0.05, 90000)["below_anchor_touch"]
    assert gapped["value"] is True
    assert gapped["first_crossing_interval"]["status"] == "ambiguous"


def test_supported_empirical_frequencies_and_freshness_gate():
    data = hours(100 * 24)
    data = [replace(c, high=120, low=80) for c in data]
    result = analyze_windows(data, 100 * 86400, horizons=[1], thresholds=[0.05])
    assert result["anchor_price"] == 100
    assert result["anchor_kind"] == "completed_hourly_close"
    estimate = result["windows"]["1"]["0.05"]
    assert estimate["status"] == "experimental"
    assert estimate["conditional_frequencies"]["below_anchor_touch"] == 1
    assert estimate["reference_frequencies"]["below_anchor_touch"] == 1
    assert estimate["conditional_frequencies"]["endpoint_below_anchor"] == 0
    assert estimate["raw_samples"] >= estimate["effective_samples"] >= 20
    assert estimate["uncertainty"]["below_anchor_touch"][0] < 1
    assert result["expires_at"] == "1970-04-11T02:00:00+00:00"
    stale = analyze_windows(data, 100 * 86400 + 7201, horizons=[1], thresholds=[0.05])
    assert stale["stale"] is True
    assert stale["windows"]["1"]["0.05"]["status"] == "stale"
    assert stale["windows"]["1"]["0.05"]["conditional_frequencies"] is None
    assert stale["windows"]["1"]["0.05"]["reference_frequencies"]["below_anchor_touch"] == 1


def test_delayed_issuance_exposes_different_empirical_horizon_and_empty_support():
    data = hours(10 * 24)
    result = analyze_windows(data, 10 * 86400, [1], [0.05], issued_at=10 * 86400 + 120)
    window = result["windows"]["1"]["0.05"]
    assert window["status"] == "insufficient"
    assert window["conditional_frequencies"] is None
    assert window["historical_alignment"] == "anchor_relative_complete_24h_bars"
    assert window["purchase_window_end"] != window["estimate_window_end"]
    assert window["issuance_alignment_matches"] is False
    assert result["issued_at"] == "1970-01-11T00:02:00+00:00"
    assert analyze_windows([], 0)["status"] == "unavailable"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"as_of": True},
        {"horizons": [True]},
        {"horizons": [0]},
        {"horizons": [366]},
        {"thresholds": [True]},
        {"thresholds": [float("nan")]},
        {"thresholds": [0]},
        {"thresholds": [1]},
        {"max_price_age": False},
        {"issued_at": -1},
    ],
)
def test_bounded_request_validation(kwargs):
    args = {"candles": [], "as_of": 0, **kwargs}
    with pytest.raises(ValueError):
        analyze_windows(**args)


def test_resolver_rejects_bad_anchor_and_future_issuance():
    with pytest.raises(ValueError):
        resolve_window([], float("nan"), 0, 0, 1, 0.05, 0)
    with pytest.raises(ValueError):
        resolve_window([], 100, 3600, 0, 1, 0.05, 3600)


def test_partial_recent_day_does_not_relabel_older_estimate_as_current_anchor():
    data = hours(100 * 24)
    data = [c for c in data if c.open_time != 99 * 86400]
    result = analyze_windows(data, 100 * 86400, [1], [0.05])
    estimate = result["windows"]["1"]["0.05"]
    assert result["anchor_time"] == "1970-04-11T00:00:00+00:00"
    assert estimate["status"] == "insufficient"
    assert estimate["conditional_frequencies"] is None
    assert estimate["estimate_anchor_time"] != result["anchor_time"]


def test_sparse_reference_rates_remain_visible_without_conditional_signal():
    result = analyze_windows(hours(70 * 24), 70 * 86400, [1], [0.05])
    estimate = result["windows"]["1"]["0.05"]
    assert estimate["status"] == "insufficient"
    assert 0 < estimate["effective_samples"] < 20
    assert estimate["conditional_frequencies"] is None
    assert estimate["reference_frequencies"]["endpoint_below_anchor"] == 0
    assert estimate["reference_uncertainty"]["endpoint_below_anchor"][1] > 0


def test_uncertain_earlier_straddle_does_not_narrow_first_confirmed_crossing():
    data = hours(26)
    data[0] = replace(data[0], low=90)
    data[5] = replace(data[5], low=90)
    event = resolve_window(data, 100, 0, 120, 1, 0.05, 100000)["below_anchor_touch"]
    assert event["value"] is True
    assert event["first_crossing_interval"]["start"] == "1970-01-01T00:02:00+00:00"
    assert event["first_crossing_interval"]["end"] == "1970-01-01T06:00:00+00:00"
    assert event["first_crossing_interval"]["status"] == "ambiguous"


def test_deadline_straddle_and_future_mutations_do_not_manufacture_a_touch():
    data = hours(26)
    # Only the last hour straddling the deadline reaches the threshold.
    data[24] = replace(data[24], low=90)
    result = resolve_window(data, 100, 0, 120, 1, 0.05, 100000)
    assert result["below_anchor_touch"]["state"] == "ambiguous"
    cutoff_result = resolve_window(data, 100, 0, 120, 1, 0.05, 86400)
    changed = [replace(c, low=50) if c.close_time > 86400 else c for c in data]
    assert resolve_window(changed, 100, 0, 120, 1, 0.05, 86400) == cutoff_result


def test_finite_json_and_default_bounded_event_grid():
    import json

    from crypto_market_analysis.context import market_context

    data = hours(10 * 24)
    result = analyze_windows(data, 10 * 86400)
    assert set(result["windows"]) == {"1", "3", "7", "14"}
    assert set(result["windows"]["1"]) == {"0.02", "0.05", "0.1"}
    json.dumps(result, allow_nan=False)
    json.dumps(market_context(data, 10 * 86400), allow_nan=False)
    json.dumps(resolve_window(data, 100, 0, 120, 1, 0.05, 10 * 86400), allow_nan=False)


def test_expiry_is_exclusive_at_maximum_price_age():
    data = hours(100 * 24)
    result = analyze_windows(data, 100 * 86400 + 7200, [1], [0.05])
    assert result["stale"] is True
    assert result["windows"]["1"]["0.05"]["conditional_frequencies"] is None


@pytest.mark.parametrize(
    "days,horizon,missing_days,missing_hours,selected,raw_count,anchor_day",
    [
        (61, 1, (), (), [], 0, 61),  # Feature warmup alone gives no purged outcomes.
        (81, 1, (), (), list(range(60, 79)), 19, 81),
        (82, 1, (), (), list(range(60, 80)), 20, 82),
        (121, 3, (), (), list(range(60, 115, 3)), 57, 121),
        (122, 3, (), (), list(range(60, 118, 3)), 58, 122),
        # A missing day excludes outcome endpoints and the following feature warmup.
        (180, 3, (75,), (), [60, 63, 66, 69, *range(136, 176, 3)], 52, 180),
        (180, 7, (75,), (), [60, 67, *range(136, 172, 7)], 44, 180),
        # A single missing endpoint hour invalidates that whole daily outcome too.
        (180, 3, (), (75 * 24 + 23,), [60, 63, 66, 69, *range(136, 176, 3)], 52, 180),
        # A partial latest day retains older evidence but cannot issue a current conditional.
        (180, 3, (75,), (179 * 24,), [60, 63, 66, 69, *range(136, 175, 3)], 51, 179),
        (180, 1, (150,), (), [], 0, 180),  # Current features are incomplete.
    ],
)
def test_public_empirical_evidence_selection(
    days, horizon, missing_days, missing_hours, selected, raw_count, anchor_day
):
    from datetime import UTC, datetime
    from math import sqrt

    from crypto_market_analysis.research import forecast

    def price(day):
        return 100 + (day * 11 % 17)

    data = [
        replace(c, open=price(c.open_time // 86400), close=price(c.open_time // 86400),
                high=price(c.open_time // 86400) + 1, low=price(c.open_time // 86400) - 1)
        for c in hours(days * 24)
        if c.open_time // 86400 not in missing_days and c.open_time // 3600 not in missing_hours
    ]
    historical = forecast(data, [horizon], threshold=0.05, as_of=days * 86400)
    research = historical["horizons"][str(horizon)]
    window = analyze_windows(data, days * 86400, [horizon], [0.05])["windows"][str(horizon)]["0.05"]
    assert research["training_samples"] == window["raw_samples"] == raw_count
    assert research["effective_samples"] == window["effective_samples"] == len(selected)
    mapping = {"red": "endpoint_below_anchor", "down_barrier": "below_anchor_touch"}
    if not selected:
        assert research["reference_frequencies"] is None
        assert research["reference_confidence_intervals"] is None
        assert window["reference_frequencies"] is None
        return
    assert research["training_start"] == datetime.fromtimestamp((selected[0] + 1) * 86400, UTC).isoformat()
    assert research["training_end"] == datetime.fromtimestamp((selected[-1] + horizon + 1) * 86400, UTC).isoformat()
    assert research["training_end"] < historical["anchor_time"]  # Strict horizon purge.
    assert window["training_start"] == research["training_start"]
    assert window["training_end"] == research["training_end"]
    for event, renamed in mapping.items():
        count = sum(
            price(day + horizon) < price(day) if event == "red" else
            min(price(future) - 1 for future in range(day + 1, day + horizon + 1)) <= price(day) * 0.95
            for day in selected
        )
        frequency = count / len(selected)
        assert research["reference_frequencies"][event] == window["reference_frequencies"][renamed] == frequency
        # Independent Wilson score formula, including nonzero uncertainty for zero counts.
        z = 1.959963984540054
        n = len(selected)
        center = (count + z * z / 2) / (n + z * z)
        radius = z * sqrt(count * (n - count) / n + z * z / 4) / (n + z * z)
        assert research["reference_confidence_intervals"][event] == pytest.approx([center - radius, center + radius])
        assert window["reference_uncertainty"][renamed] == research["reference_confidence_intervals"][event]
    if len(selected) < 20:
        assert research["base_rates"] is None
        assert research["probabilities"] is None
        assert window["conditional_frequencies"] is None
    else:
        assert research["base_rates"] == research["reference_frequencies"]
        assert window["conditional_frequencies"] == {
            renamed: research["probabilities"][event] for event, renamed in mapping.items()
        }
    assert historical["anchor_time"] == datetime.fromtimestamp(anchor_day * 86400, UTC).isoformat()


def test_sparse_evidence_excludes_future_closes_and_late_observations():
    from crypto_market_analysis.research import forecast

    cutoff = 81 * 86400
    data = hours(90 * 24)
    # A past extreme not known at the cutoff must not enter the sample.
    data[10 * 24] = replace(data[10 * 24], low=1, observed_at=cutoff + 1)
    changed = [
        replace(c, close=500, high=500, low=1)
        if c.close_time > cutoff or c.observed_at > cutoff else c
        for c in data
    ]
    evidence = forecast(data, [1, 3], as_of=cutoff)
    assert evidence["horizons"]["1"]["effective_samples"] == 8
    assert evidence["horizons"]["1"]["reference_frequencies"] is not None
    assert evidence == forecast(changed, [1, 3], as_of=cutoff)
    assert analyze_windows(data, cutoff, [1, 3], [0.05]) == analyze_windows(changed, cutoff, [1, 3], [0.05])
    # The cutoff excludes future hours even when supplied without the rest of the future history.
    known = [c for c in data if c.close_time <= cutoff and c.observed_at <= cutoff]
    assert forecast(data, [1, 3], as_of=cutoff) == forecast(known, [1, 3], as_of=cutoff)
    assert analyze_windows(data, cutoff, [1, 3], [0.05]) == analyze_windows(known, cutoff, [1, 3], [0.05])
