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


def ecb_schedule(rows=None, **overrides):
    import json

    from crypto_market_analysis.calendars import ECB_URL

    return json.dumps({
        "schema_version": "ecb-manual-calendar-v1", "source_url": ECB_URL,
        "provenance": {"checked_at": 100, "note": "Synthetic transcription example; not fetched."},
        "events": rows if rows is not None else [{
            "id": "synthetic-ecb-policy", "title": "Synthetic ECB policy meeting",
            "event_kind": "policy_meeting", "local_date": "2026-10-29", "sequence": 0,
        }], **overrides,
    })


def test_ecb_manual_date_only_has_honest_provenance_and_stable_revision_identity():
    import json

    from crypto_market_analysis.calendars import ECB_URL, parse_ecb_manual_schedule

    text = ecb_schedule()
    [event] = parse_ecb_manual_schedule(text, 200)
    assert event["source_url"] == ECB_URL
    assert event["origin"] == "sourced_manual_schedule"
    assert event["time_status"] == "date_only" and event["event_time"] is None
    assert event["publication_time"] is None and not event["publication_time_verified"]
    assert event["historical_known_at"] == "unknown"
    revised = json.loads(text)
    revised["events"][0].update(sequence=1, status="cancelled")
    [revision] = parse_ecb_manual_schedule(json.dumps(revised), 300)
    assert revision["event_id"] == event["event_id"]
    assert revision["data_version"] != event["data_version"]
    assert revision["status"] == "cancelled" and revision["sequence"] == 1


@pytest.mark.parametrize(("timestamp", "zone", "expected"), [
    ("2026-01-22T14:45:00", "Europe/Frankfurt", "2026-01-22T13:45:00"),
    ("2026-07-23T14:45:00", "Europe/Berlin", "2026-07-23T12:45:00"),
    ("2026-07-23T14:45:00+02:00", None, "2026-07-23T12:45:00"),
])
def test_ecb_explicit_conference_schedule_respects_frankfurt_dst(timestamp, zone, expected):
    from crypto_market_analysis.calendars import parse_ecb_manual_schedule

    row = {"id": "synthetic-press", "title": "Synthetic conference", "event_kind": "press_conference",
           "local_date": timestamp[:10], "sequence": 0, "scheduled_time": timestamp}
    if zone:
        row["timezone"] = zone
    [event] = parse_ecb_manual_schedule(ecb_schedule([row]), 100)
    assert event["event_time"] == epoch(expected)
    assert event["event_time_verified"] and event["publication_status"] == "unknown"


@pytest.mark.parametrize(("timestamp", "zone", "status"), [
    ("2026-03-29T02:30:00", "Europe/Berlin", "ambiguous_local_time"),
    ("2026-10-25T02:30:00", "Europe/Frankfurt", "ambiguous_local_time"),
    ("2026-07-23T14:45:00", None, "unsupported_explicit_time"),
    ("2026-07-23T14:45:00", "Europe/Paris", "unsupported_timezone"),
    ("2026-07-23T14:45:00+01:00", "Europe/Berlin", "unsupported_explicit_time"),
    ("2026-07-23", "Europe/Berlin", "unsupported_explicit_time"),
])
def test_ecb_unsupported_times_remain_explicit(timestamp, zone, status):
    from crypto_market_analysis.calendars import parse_ecb_manual_schedule

    row = {"id": "synthetic-press", "title": "Synthetic conference", "event_kind": "press_conference",
           "local_date": timestamp[:10], "sequence": 0, "scheduled_time": timestamp}
    if zone:
        row["timezone"] = zone
    [event] = parse_ecb_manual_schedule(ecb_schedule([row]), 100)
    assert event["time_status"] == status
    assert event["event_time"] is None and not event["event_time_verified"]


def test_ecb_manual_validation_duplicates_revisions_and_bounds():
    import json

    from crypto_market_analysis.calendars import parse_ecb_manual_schedule

    row = json.loads(ecb_schedule())["events"][0]
    with pytest.raises(ValueError, match="Duplicate ECB"):
        parse_ecb_manual_schedule(ecb_schedule([row, row]), 100)
    revisions = parse_ecb_manual_schedule(ecb_schedule([row, {**row, "sequence": 1}]), 100)
    assert len(revisions) == 2 and revisions[0]["event_id"] == revisions[1]["event_id"]
    with pytest.raises(ValueError, match="Duplicate JSON"):
        parse_ecb_manual_schedule('{"events":[],"events":[]}', 100)
    with pytest.raises(ValueError, match="two-megabyte"):
        parse_ecb_manual_schedule("x" * (2 * 1024 * 1024 + 1), 100)
    with pytest.raises(ValueError, match="2000"):
        parse_ecb_manual_schedule(ecb_schedule([row] * 2001), 100)
    for payload in (
        ecb_schedule(source_url="https://example.invalid/ecb"),
        ecb_schedule(provenance={"checked_at": 101, "note": "future"}),
        ecb_schedule([{**row, "publication_time": 100}]),
        ecb_schedule([{**row, "source_last_modified": "2026-01-01T00:00:00"}]),
    ):
        with pytest.raises(ValueError):
            parse_ecb_manual_schedule(payload, 100)
    [unsupported] = parse_ecb_manual_schedule(ecb_schedule([{**row, "event_kind": "non_policy_meeting"}]), 100)
    assert unsupported["time_status"] == "unsupported_event_kind"


def test_ecb_shareable_example_is_explicitly_synthetic_and_sourced_manual():
    from pathlib import Path

    from crypto_market_analysis.calendars import parse_ecb_manual_schedule

    text = (Path(__file__).parents[1] / "examples" / "ecb-manual-calendar.json").read_text()
    rows = parse_ecb_manual_schedule(text, 100)
    assert len(rows) == 2
    assert all(row["origin"] == "sourced_manual_schedule" for row in rows)
    assert all("SYNTHETIC" in row["provenance"]["note"] for row in rows)


def test_ecb_revision_order_does_not_let_older_input_override_newer_revision():
    import json

    from crypto_market_analysis.calendars import parse_ecb_manual_schedule

    row = json.loads(ecb_schedule())["events"][0]
    revisions = parse_ecb_manual_schedule(ecb_schedule([
        {**row, "sequence": 2, "status": "cancelled"}, row,
        {**row, "sequence": 1, "source_last_modified": "1970-01-01T00:01:00Z"},
    ]), 100)
    assert [item["sequence"] for item in revisions] == [0, 1, 2]
    assert revisions[-1]["status"] == "cancelled"
    assert revisions[1]["source_last_modified"] == "1970-01-01T00:01:00Z"
