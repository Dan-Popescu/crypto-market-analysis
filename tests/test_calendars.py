"""Hand-authored public-schema fixtures; no fetched calendars."""

from datetime import UTC, datetime

import pytest

from crypto_market_analysis.calendars import collect_calendars, parse_bls_ics, parse_fomc_calendar


def epoch(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())


def ics(start, extra="", title="Consumer Price Index"):
    return (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:synthetic-cpi\r\nSEQUENCE:2\r\n"
        f"SUMMARY:{title}\r\nDTSTART{start}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR"
    )


@pytest.mark.parametrize(
    ("local", "utc"),
    [
        ("20260114T083000", "2026-01-14T13:30:00"),
        ("20260714T083000", "2026-07-14T12:30:00"),
    ],
)
def test_cpi_eastern_dst_scheduled_time_is_not_publication(local, utc):
    [event] = parse_bls_ics(ics(f";TZID=America/New_York:{local}"), observed_at=100)
    assert event["event_time"] == epoch(utc)
    assert event["original_timezone"] == "America/New_York"
    assert event["original_time"] == local
    assert event["event_time_verified"] is True
    assert event["status"] == "planned" and event["publication_status"] == "unknown"
    assert event["publication_time"] is None and not event["publication_time_verified"]
    assert event["observed_at"] == 100 and event["sequence"] == "2"


def test_ics_revision_identity_and_folded_title_preserved():
    text = ics(":20260114T083000", "LAST-MODIFIED:20260101T120000Z\r\n", "Consumer Price \r\n Index")
    [event] = parse_bls_ics(text, 100)
    [revised] = parse_bls_ics(text.replace("SEQUENCE:2", "SEQUENCE:3"), 200)
    assert event["source_event_id"] == revised["source_event_id"] == "synthetic-cpi"
    assert event["data_version"] != revised["data_version"]
    assert event["source_last_modified"] == "20260101T120000Z"
    assert event["event_time"] == epoch("2026-01-14T13:30:00")
    assert parse_bls_ics(ics(":20260114T083000", title="Synthetic unrelated release"), 100) == []


@pytest.mark.parametrize(
    ("start", "extra", "status"),
    [
        (";VALUE=DATE:20260114", "", "date_only"),
        (";TZID=Europe/Paris:20260114T083000", "", "unsupported_timezone"),
        (";TZID=America/New_York:20261101T013000", "", "ambiguous_local_time"),
        (";TZID=America/New_York:20260308T023000", "", "ambiguous_local_time"),
        (":20260114T083000", "RRULE:FREQ=MONTHLY\r\n", "unsupported_recurrence"),
    ],
)
def test_uncertain_times_are_not_guessed(start, extra, status):
    [event] = parse_bls_ics(ics(start, extra), 100)
    assert event["event_time"] is None and not event["event_time_verified"]
    assert event["time_status"] == status


def fed_row(dates="27-28", extra=""):
    return (
        '<h4 class="panel-title"><a>2026 FOMC Meetings</a></h4>'
        '<div class="row fomc-meeting"><div class="fomc-meeting__month">October</div>'
        f'<div class="fomc-meeting__date">{dates}</div>{extra}'
        '<a href="/statement.htm">Statement</a></div>'
    )


def test_fomc_meeting_dates_do_not_invent_decision_time():
    [event] = parse_fomc_calendar(fed_row(), 100)
    assert event["meeting_start_date"] == "2026-10-27"
    assert event["meeting_end_date"] == "2026-10-28"
    assert event["event_time"] is None
    assert event["time_status"] == "date_only"
    assert not event["publication_time_verified"]


def test_explicit_fomc_time_requires_timezone_and_matching_end_date():
    [event] = parse_fomc_calendar(fed_row(extra='<time datetime="2026-10-28T14:00:00-04:00"/>'), 100)
    assert event["event_time"] == epoch("2026-10-28T18:00:00")
    assert event["event_time_verified"]
    for timestamp in ("2026-10-28T14:00:00", "2026-10-27T14:00:00-04:00"):
        [event] = parse_fomc_calendar(fed_row(extra=f'<time datetime="{timestamp}"/>'), 100)
        assert event["event_time"] is None
        assert event["time_status"] == "unsupported_explicit_time"


def test_calendar_collection_partial_failure_is_explicit():
    class Client:
        def get_text(self, url):
            if "bls.gov" in url:
                raise OSError("synthetic unavailable source")
            return fed_row()

    result = collect_calendars(100, client=Client())
    assert len(result["events"]) == 1
    assert result["errors"][0]["source"] == "bls"
    assert result["errors"][0]["status"] == "unavailable"


def test_calendar_size_limit_and_duplicate_fields():
    with pytest.raises(ValueError, match="two-megabyte"):
        parse_bls_ics("x" * (2 * 1024 * 1024 + 1), 100)
    with pytest.raises(ValueError, match="Duplicate ICS"):
        parse_bls_ics(ics(":20260114T083000", "UID:second\r\n"), 100)


def test_cancelled_utc_cpi_entry_retains_schedule_without_publication_claim():
    [event] = parse_bls_ics(ics(":20260714T123000Z", "STATUS:CANCELLED\r\n"), 100)
    assert event["status"] == "cancelled"
    assert event["original_timezone"] == "UTC"
    assert event["event_time"] == epoch("2026-07-14T12:30:00")
    assert event["publication_status"] == "unknown"
