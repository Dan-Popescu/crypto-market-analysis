"""Synthetic imported schedules and established offline exchange rule checks."""

from copy import deepcopy
from datetime import datetime

import pytest

from crypto_market_analysis.cross_asset.sessions import (
    coverage_report,
    expected_observations,
    generate_exchange_calendar,
    validate_calendar,
)


def ts(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def segment(opening, closing):
    return {"start": ts(opening), "end": ts(closing)}


def calendar(sessions=(), **extra):
    return {"schema": "market-calendar.v1", "id": "synthetic-XNYS", "version": "fixture-v1",
            "timezone": "America/New_York", "source_url": "https://example.invalid/synthetic-schedule",
            "known_at": ts("2026-01-01T00:00Z"), "supported_start": ts("2026-01-01T00:00-05:00"),
            "supported_end": ts("2027-01-01T00:00-05:00"), "profile": "explicit",
            "sessions": list(sessions), **extra}


def ny_session(day, offset="-04:00", closing="16:00"):
    return {"date": day, "segments": [segment(f"{day}T09:30{offset}", f"{day}T{closing}{offset}")]}


def query(document, start, end, as_of="2026-12-31T00:00Z", interval="session"):
    return expected_observations(document, start=ts(start), end=ts(end), as_of=ts(as_of), interval=interval)


def test_native_hourly_bounds_shift_with_dst_and_preserve_partial_close():
    document = calendar([ny_session("2026-03-06", "-05:00"), ny_session("2026-03-09")])
    rows = query(document, "2026-03-06T00:00Z", "2026-03-10T00:00Z", interval="1h")
    assert len(rows) == 14
    assert rows[0]["start"] == ts("2026-03-06T14:30Z")
    assert rows[7]["start"] == ts("2026-03-09T13:30Z")
    assert rows[6]["end"] - rows[6]["start"] == 1800
    assert rows[-1]["end"] == ts("2026-03-09T20:00Z")


def test_holiday_weekend_and_short_session_have_distinct_expected_coverage():
    document = calendar([ny_session("2026-11-27", "-05:00", "13:00")])
    report = coverage_report(document, [], start=ts("2026-11-26T00:00-05:00"),
                             end=ts("2026-11-30T00:00-05:00"), as_of=ts("2026-12-01T00:00Z"), interval="1h")
    assert report["counts"]["missing"] == 4
    assert report["observations"][-1]["end"] == ts("2026-11-27T18:00Z")
    assert report["closed_seconds"] == 4 * 86400 - int(3.5 * 3600)


def test_lunch_break_does_not_generate_bars_or_discard_native_session_segments():
    document = calendar([{"date": "2026-06-01", "segments": [
        segment("2026-06-01T09:00+09:00", "2026-06-01T11:30+09:00"),
        segment("2026-06-01T12:30+09:00", "2026-06-01T15:30+09:00")]}], timezone="Asia/Tokyo")
    rows = query(document, "2026-06-01T00:00Z", "2026-06-01T07:00Z", interval="1h")
    assert len(rows) == 6
    assert rows[2]["end"] == ts("2026-06-01T02:30Z")
    assert rows[3]["start"] == ts("2026-06-01T03:30Z")
    [daily] = query(document, "2026-06-01T00:00Z", "2026-06-01T07:00Z")
    assert len(daily["segments"]) == 2
    report = coverage_report(document, [], start=ts("2026-06-01T00:00Z"), end=ts("2026-06-01T07:00Z"),
                             as_of=ts("2026-06-02T00:00Z"))
    assert {"start": ts("2026-06-01T02:30Z"), "end": ts("2026-06-01T03:30Z"), "status": "closed"} in report["closed_intervals"]


def test_delayed_reference_publication_is_not_missing_before_due_time():
    document = calendar([{**ny_session("2026-06-01"), "publication_at": ts("2026-06-01T21:00Z")}],
                        provider_delay_seconds=900)
    before = query(document, "2026-06-01T00:00Z", "2026-06-02T00:00Z", "2026-06-01T21:14:59Z", "reference")
    assert before[0]["status"] == "not_yet_due"
    assert before[0]["due_at"] == ts("2026-06-01T21:15Z")
    after = coverage_report(document, [], start=ts("2026-06-01T00:00Z"), end=ts("2026-06-02T00:00Z"),
                            as_of=ts("2026-06-01T21:15Z"), interval="reference")
    assert after["counts"]["missing"] == 1


def test_publication_only_grid_has_explicit_point_bounds():
    publication = ts("2026-06-01T14:00Z")
    document = calendar([{"date": "2026-06-01", "segments": [], "publication_at": publication}])
    [row] = query(document, "2026-06-01T00:00Z", "2026-06-02T00:00Z", interval="reference")
    assert row["start"] == row["end"] == publication
    with pytest.raises(ValueError, match="reference"):
        query(document, "2026-06-01T00:00Z", "2026-06-02T00:00Z")


def test_future_receipt_cannot_repair_historical_missing_or_stale_bar():
    document = calendar([ny_session("2026-06-01")])
    native = document["sessions"][0]["segments"][0]
    args = {"start": ts("2026-06-01T00:00Z"), "end": ts("2026-06-02T00:00Z")}
    complete = {**native, "observed_at": ts("2026-06-01T20:05Z")}
    assert coverage_report(document, [complete], **args, as_of=ts("2026-06-01T20:00Z"))["counts"]["missing"] == 1
    partial = {**native, "observed_at": ts("2026-06-01T19:59Z")}
    report = coverage_report(document, [partial, complete], **args, as_of=ts("2026-06-01T20:00Z"))
    assert report["counts"]["stale"] == 1
    assert coverage_report(document, [partial, complete], **args, as_of=complete["observed_at"])["counts"]["observed"] == 1


def test_bar_at_session_close_is_complete_but_earlier_receipt_is_stale():
    document = calendar([ny_session("2026-06-01")])
    native = document["sessions"][0]["segments"][0]
    bar = {**native, "observed_at": native["end"]}
    report = coverage_report(document, [bar], start=native["start"], end=native["end"], as_of=native["end"])
    assert report["counts"]["observed"] == 1
    bar["status"] = "stale"
    assert coverage_report(document, [bar], start=native["start"], end=native["end"], as_of=native["end"])["counts"]["stale"] == 1


def test_halts_require_visible_sourced_status_and_remain_distinct_from_closed():
    halt = {"start": ts("2026-06-01T14:00Z"), "end": ts("2026-06-01T14:30Z"),
            "known_at": ts("2026-06-02T00:00Z"), "source_url": "https://example.invalid/synthetic-halt"}
    document = calendar([ny_session("2026-06-01")], halts=[halt])
    args = {"start": ts("2026-06-01T00:00Z"), "end": ts("2026-06-02T00:00Z")}
    assert coverage_report(document, [], **args, as_of=ts("2026-06-01T21:00Z"))["counts"]["missing"] == 1
    report = coverage_report(document, [], **args, as_of=halt["known_at"])
    assert report["counts"]["halted"] == 1
    assert report["counts"]["missing"] == 0


def test_unknown_range_and_later_known_calendar_are_never_closed():
    document = calendar([])
    report = coverage_report(document, [], start=ts("2025-12-31T00:00Z"), end=ts("2026-01-02T00:00Z"),
                             as_of=ts("2026-01-03T00:00Z"))
    assert report["counts"]["unknown"] == 1
    assert report["closed_seconds"] == 19 * 3600
    prior = coverage_report(document, [], start=ts("2025-12-31T00:00Z"), end=ts("2026-01-02T00:00Z"),
                            as_of=ts("2025-12-31T12:00Z"))
    assert prior["counts"]["unknown"] == 1 and prior["closed_seconds"] == 0


def test_query_preserves_bar_bounds_instead_of_fabricating_truncated_bar():
    document = calendar([ny_session("2026-06-01")])
    [row] = query(document, "2026-06-01T15:00Z", "2026-06-01T16:00Z")
    assert row["start"] == ts("2026-06-01T13:30Z")
    assert row["end"] == ts("2026-06-01T20:00Z")


def test_continuous_crypto_grid_has_24_hours_and_no_weekend_closure():
    document = calendar(profile="continuous", timezone="UTC", supported_start=ts("2026-01-01T00:00Z"),
                        supported_end=ts("2027-01-01T00:00Z"))
    report = coverage_report(document, [], start=ts("2026-06-06T00:00Z"), end=ts("2026-06-07T00:00Z"),
                             as_of=ts("2026-06-08T00:00Z"), interval="1h")
    assert report["counts"]["missing"] == 24 and report["closed_seconds"] == 0


def test_continuous_local_days_follow_23_and_25_hour_dst_days():
    document = calendar(profile="continuous")
    spring = query(document, "2026-03-08T00:00-05:00", "2026-03-09T00:00-04:00", interval="1h")
    autumn = query(document, "2026-11-01T00:00-04:00", "2026-11-02T00:00-05:00", interval="1h")
    assert len(spring) == 23 and len(autumn) == 25


def test_continuous_partial_supported_day_is_unknown_not_closed():
    document = calendar(profile="continuous", timezone="UTC", supported_start=ts("2026-01-01T12:00Z"))
    report = coverage_report(document, [], start=ts("2026-01-01T12:00Z"), end=ts("2026-01-02T00:00Z"),
                             as_of=ts("2026-06-01T00:00Z"))
    assert report["counts"]["unknown"] == 1 and report["closed_seconds"] == 0


def test_short_continuous_support_does_not_duplicate_unknown_ranges():
    document = calendar(profile="continuous", timezone="UTC", supported_start=ts("2026-01-01T12:00Z"),
                        supported_end=ts("2026-01-01T18:00Z"))
    [row] = query(document, "2026-01-01T00:00Z", "2026-01-02T00:00Z")
    assert row["status"] == "unknown"
    assert row["start"] == ts("2026-01-01T00:00Z") and row["end"] == ts("2026-01-02T00:00Z")


def test_halt_only_during_lunch_does_not_mark_a_traded_session_halted():
    document = calendar([{"date": "2026-06-01", "segments": [
        segment("2026-06-01T09:00+09:00", "2026-06-01T11:30+09:00"),
        segment("2026-06-01T12:30+09:00", "2026-06-01T15:30+09:00")]}], timezone="Asia/Tokyo",
        halts=[{"start": ts("2026-06-01T02:45Z"), "end": ts("2026-06-01T03:00Z"),
                "known_at": ts("2026-06-01T02:45Z"), "source_url": "https://example.invalid/halt"}])
    [row] = query(document, "2026-06-01T00:00Z", "2026-06-02T00:00Z")
    assert row["status"] == "expected"


@pytest.mark.parametrize(("field", "value"), [
    ("known_at", True), ("known_at", 1.2), ("timezone", "Not/AZone"),
    ("profile", "weekdays"), ("provider_delay_seconds", -1), ("source_url", "http://example.invalid"),
    ("sessions", "invalid"), ("schema", "market-calendar.v9"),
])
def test_invalid_calendar_inputs_fail_explicitly(field, value):
    with pytest.raises(ValueError):
        validate_calendar(calendar(**{field: value}))


def test_validation_detaches_input_and_rejects_overlaps_and_unsupported_fields():
    original = calendar([ny_session("2026-06-01")])
    canonical = validate_calendar(original)
    canonical["sessions"][0]["segments"][0]["start"] += 1
    assert original["sessions"][0]["segments"][0] != canonical["sessions"][0]["segments"][0]
    invalid = deepcopy(original)
    invalid["sessions"][0]["segments"] *= 2
    with pytest.raises(ValueError, match="nonoverlapping"):
        validate_calendar(invalid)
    with pytest.raises(ValueError, match="unsupported"):
        validate_calendar({**original, "silent_guess": True})


def test_query_limits_and_unsupported_interval_fail_explicitly():
    document = calendar(profile="continuous", timezone="UTC", supported_start=ts("2020-01-01T00:00Z"),
                        supported_end=ts("2040-01-01T00:00Z"))
    with pytest.raises(ValueError, match="10000"):
        query(document, "2026-01-01T00:00Z", "2028-01-01T00:00Z", interval="1h")
    with pytest.raises(ValueError, match="ten years"):
        query(document, "2026-01-01T00:00Z", "2038-01-01T00:00Z")
    with pytest.raises(ValueError, match="interval"):
        query(document, "2026-01-01T00:00Z", "2026-01-02T00:00Z", interval="5m")


def test_pre_epoch_historical_schedule_is_supported():
    document = calendar([ny_session("1960-06-01")], known_at=ts("1960-01-01T00:00Z"),
                        supported_start=ts("1960-01-01T00:00Z"), supported_end=ts("1961-01-01T00:00Z"))
    [row] = query(document, "1960-06-01T00:00Z", "1960-06-02T00:00Z", "1960-06-03T00:00Z")
    assert row["start"] < 0 and row["status"] == "expected"


def test_generated_nyse_rules_cover_dst_good_friday_and_early_close():
    document = generate_exchange_calendar("XNYS", start_date="2026-01-01", end_date="2026-12-31",
                                          known_at=ts("2026-10-30T00:00Z"))
    sessions = {session["date"]: session for session in document["sessions"]}
    assert "2026-04-03" not in sessions  # Good Friday, unlike a weekday rule.
    assert sessions["2026-03-06"]["segments"][0]["start"] == ts("2026-03-06T14:30Z")
    assert sessions["2026-03-09"]["segments"][0]["start"] == ts("2026-03-09T13:30Z")
    assert sessions["2026-11-27"]["segments"][0]["end"] == ts("2026-11-27T18:00Z")
    assert document["generation"]["historical_known_at_guaranteed"] is False
    assert "exchange-calendars" in document["version"]


@pytest.mark.parametrize("mic", ["XPAR", "XTKS"])
def test_generated_paris_and_tokyo_have_native_sessions(mic):
    document = generate_exchange_calendar(mic, start_date="2026-06-01", end_date="2026-06-07", known_at=0)
    session = document["sessions"][0]
    if mic == "XTKS":
        assert len(session["segments"]) == 2
        assert session["segments"][0]["end"] == ts("2026-06-01T02:30Z")
        assert session["segments"][-1]["end"] == ts("2026-06-01T06:30Z")
    else:
        assert session["segments"][0] == segment("2026-06-01T07:00Z", "2026-06-01T15:30Z")


def test_generated_weekend_only_range_is_sourced_closed_not_unknown():
    document = generate_exchange_calendar("XNYS", start_date="2026-06-06", end_date="2026-06-07", known_at=0)
    assert document["sessions"] == []
    report = coverage_report(document, [], start=document["supported_start"], end=document["supported_end"], as_of=0)
    assert report["closed_seconds"] == 2 * 86400
    assert sum(report["counts"].values()) == 0
