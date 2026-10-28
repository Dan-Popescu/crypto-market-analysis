from dataclasses import replace

import pytest

from crypto_market_analysis.models import Candle
from crypto_market_analysis.research import build_analysis, daily_bars, evaluate, forecast


def candles(days=100, start=0):
    result = []
    for d in range(days):
        price = 100 + d * 0.1
        for hour in range(24):
            t = (start + d) * 86400 + hour * 3600
            result.append(
                Candle(
                    "test", "BTC", "USD", "1h", t, t + 3600, price, price + 1, price - 1, price, 1, t + 3600
                )
            )
    return result


def test_daily_complete_duplicate_and_mixed():
    data = candles(3)
    assert len(daily_bars(data)) == 3
    assert len(daily_bars(data + [data[0]])) == 2
    assert len(daily_bars(data[1:])) == 2
    with pytest.raises(ValueError):
        daily_bars(data + [replace(data[0], quote="USDT")])


def test_asof_close_and_future_mutation():
    data = candles(110)
    cutoff = 100 * 86400
    a = forecast(data, [1], as_of=cutoff)
    changed = [replace(c, close=c.close * 10, high=c.high * 10) if c.close_time > cutoff else c for c in data]
    assert forecast(changed, [1], as_of=cutoff) == a
    assert a["coverage"]["complete_days"] == 100
    assert forecast(data, [1], as_of=cutoff - 1)["coverage"]["complete_days"] == 99


def test_gap_and_partial_week():
    data = candles(43)
    data = [c for c in data if c.open_time // 86400 != 22]
    analysis = build_analysis(data, [], 43 * 86400)
    assert analysis["evidence"]["returns"]["21"] is None
    # Epoch day 0 was Thursday; last complete week starts on day 32.
    assert analysis["evidence"]["weekly_trend"]["week_start"].startswith("1970-02-02")


def test_minimum_and_barriers():
    assert forecast(candles(70), [7])["horizons"]["7"]["status"] == "insufficient"
    data = candles(90)
    data = [replace(c, high=c.open * 1.2, low=c.open * 0.8) for c in data]
    result = forecast(data, [1])["horizons"]["1"]
    assert result["probabilities"]["up_barrier"] == 1
    assert result["probabilities"]["down_barrier"] == 1
    assert result["probabilities"]["red"] == 0


def test_evaluation_purges_and_nonoverlap():
    result = evaluate(candles(260), horizon=7)
    assert result["sample_size"] > 0
    import pandas as pd

    origins = result["origins"]
    for item in origins:
        assert pd.Timestamp(item["training_end"]) < pd.Timestamp(item["origin"])
    for first, second in zip(origins, origins[1:], strict=False):
        assert pd.Timestamp(first["outcome_end"]) <= pd.Timestamp(second["origin"])


def test_observation_groups_revision_and_unknown_unit():
    from crypto_market_analysis.models import Observation

    end = 40 * 86400
    observations = [
        Observation(
            "fred",
            "MACRO",
            "rate",
            end - 7 * 86400,
            end - 7 * 86400,
            2,
            "unknown",
            metadata={"coverage": 0.8},
        ),
        Observation(
            "fred",
            "MACRO",
            "rate",
            end,
            end,
            3,
            "unknown",
            metadata={"coverage": 0.9, "funding_interval": "8h"},
        ),
        Observation("fred", "MACRO", "rate", end, end + 1, 99, "unknown"),
    ]
    result = build_analysis(candles(40), observations, end)
    group = result["evidence"]["observations"][0]
    assert group["value"] == 3
    assert group["changes"]["7"]["absolute"] == 1
    assert group["changes"]["21"] is None
    assert group["unit_unknown"] and group["metadata"]["coverage"] == 0.9
    assert result["coverage"]["observations"]["groups_by_source"] == {"fred": 1}


def test_forecast_stale_and_wilson_interval():
    data = candles(100)
    result = forecast(data, [1, 60], as_of=104 * 86400)
    assert result["stale"] is True
    one = result["horizons"]["1"]
    low, high = one["confidence_intervals"]["red"]
    assert 0 <= low <= one["probabilities"]["red"] <= high <= 1
    assert result["horizons"]["60"]["required_effective_samples"] == 20
    assert result["horizons"]["60"]["insufficient_reason"]


def test_evaluation_versions_do_not_leak_future_revision():
    data = candles(90)
    revision = replace(data[24 * 65], observed_at=91 * 86400, high=500, close=400)
    changed_revision = replace(revision, high=900, close=800)
    first = evaluate(data + [revision], horizon=1)
    second = evaluate(data + [changed_revision], horizon=1)
    assert first["sample_size"] > 0
    assert first["origins"][0]["probabilities"] == second["origins"][0]["probabilities"]
    assert first["origins"][0]["base_rates"] == second["origins"][0]["base_rates"]
    # Duplicate versions retain complete days for outcome scoring.
    assert first["sample_size"] == evaluate(data, horizon=1)["sample_size"]


def test_backfilled_history_requires_explicit_retrospective():
    data = [replace(c, observed_at=300 * 86400) for c in candles(260)]
    strict = evaluate(data, horizon=7)
    retro = evaluate(data, horizon=7, validation="retrospective")
    assert strict["sample_size"] == 0
    assert strict["status"] == "insufficient"
    assert retro["sample_size"] > 0
    assert retro["validation_mode"] == "retrospective"
    assert not retro["historical_availability_verified"]


def test_endpoint_direction_and_anchor():
    result = forecast(candles(90), [1])
    assert result["anchor_price"] == pytest.approx(108.9)
    assert result["anchor_time"].startswith("1970-04-01")
    p = result["horizons"]["1"]["probabilities"]
    assert p["up"] == 1 and p["red"] == 0 and p["flat"] == 0


def test_calendar_week_candle_is_complete_and_separate():
    data = candles(12)
    # One complete Monday-to-Sunday week, followed by a partial week.
    weekly = build_analysis(data, [], 12 * 86400)["evidence"]["last_completed_week_candle"]
    assert weekly["start"].startswith("1970-01-05")
    assert weekly["end"].startswith("1970-01-12")
    assert weekly["color"] == "green"
    missing = [c for c in data if c.open_time // 86400 != 5]
    assert build_analysis(missing, [], 12 * 86400)["evidence"]["last_completed_week_candle"] is None


def test_delayed_availability_and_anchor_revision():
    import pandas as pd

    data = [replace(c, observed_at=c.close_time + 120) for c in candles(90)]
    baseline = evaluate(data, horizon=1)
    assert baseline["sample_size"] > 0
    origin = baseline["origins"][0]
    assert origin["data_availability_delay_seconds"] == 120
    assert pd.Timestamp(origin["issued_at"]) - pd.Timestamp(origin["anchor_time"]) == pd.Timedelta(
        seconds=120
    )
    assert pd.Timestamp(origin["outcome_end"]) == pd.Timestamp(origin["anchor_time"]) + pd.Timedelta(days=1)
    anchor_ts = int(pd.Timestamp(origin["anchor_time"]).timestamp())
    old = next(c for c in data if c.close_time == anchor_ts)
    revision = replace(old, close=old.close * 3, high=old.high * 3, observed_at=anchor_ts + 121)
    revised = evaluate(data + [revision], horizon=1)["origins"][0]
    assert revised["anchor_price"] == origin["anchor_price"]
    assert revised["probabilities"] == origin["probabilities"]
    assert revised["outcomes"] == origin["outcomes"]


def test_forecast_aligned_to_last_closed_hour_and_future_isolation():
    data = candles(105)
    cutoff = 100 * 86400 + 17 * 3600
    result = forecast(data, [1], as_of=cutoff)
    import pandas as pd

    assert pd.Timestamp(result["anchor_time"]).timestamp() == cutoff
    assert result["bar_alignment"] == "rolling_24h_at_last_hourly_close"
    future_modified = [
        replace(c, close=c.close * 5, high=c.high * 5) if c.close_time > cutoff else c for c in data
    ]
    assert forecast(future_modified, [1], as_of=cutoff) == result


def test_evaluation_delayed_aligned_hour():
    import pandas as pd

    data = [replace(c, observed_at=c.close_time + 120) for c in candles(110)]
    result = evaluate(data, horizon=1, anchor_hour_utc=17)
    assert result["sample_size"] > 0
    assert result["anchor_hour_utc"] == 17
    assert pd.Timestamp(result["origins"][0]["anchor_time"]).hour == 17
    assert result["origins"][0]["data_availability_delay_seconds"] == 120


@pytest.mark.parametrize("direction,close", [("up", 200), ("down", 50), ("flat", 100)])
def test_analysis_direction_values_are_english(direction, close):
    data = [replace(c, close=close, high=max(c.high, close), low=min(c.low, close)) for c in candles(43)]
    # The first 22 days provide a distinct baseline; the remaining period sets direction.
    data = [replace(c, close=100) if c.open_time < 22 * 86400 else c for c in data]
    analysis = build_analysis(data, [], 43 * 86400)
    assert analysis["evidence"]["daily_trend"] == direction
    # A constant close across complete weeks is a flat weekly trend.
    assert analysis["evidence"]["weekly_trend"]["direction"] == "flat"
    assert f"21-day trend: {direction}." in analysis["facts"]


def test_imported_text_and_legacy_unknown_unit_are_preserved():
    from crypto_market_analysis.models import Observation

    original = Observation(
        "feed", "BTC", "metric", 0, 0, 1, "inconnu", metadata={"provider_note": "Donnée brute"}
    )
    point = build_analysis([], [original], 0)["evidence"]["observations"][0]
    assert point["unit"] == "inconnu" and point["unit_unknown"]
    assert point["metadata"]["provider_note"] == "Donnée brute"
