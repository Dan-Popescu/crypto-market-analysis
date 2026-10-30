"""Synthetic markup around observed official rendered wording; not a live HTML fixture."""

import hashlib

import pytest

from crypto_market_analysis.calendars import ECB_URL, collect_calendars, parse_ecb_calendar

HEADING = "Schedules for the meetings of the Governing Council and General Council of the ECB and related press conferences"
TITLE = "Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference"


def html(day="29/10/2026", title=TITLE):
    return f"<h1>{HEADING}</h1><dl><dt>{day}</dt><dd>{title}</dd></dl><h3>All pages in this section</h3>"


@pytest.mark.parametrize("day", ["29/01/2026", "23/07/2026"])
def test_automatic_calendar_keeps_winter_and_summer_dates_without_clock_invention(day):
    [row] = parse_ecb_calendar(html(day), 100)
    assert row["event_kind"] == "policy_meeting" and row["time_status"] == "date_only"
    assert row["event_time"] is None and not row["event_time_verified"]
    assert row["publication_time"] is None and not row["publication_time_verified"]
    assert row["original_timezone"] == "Europe/Berlin" and row["origin"] == "official_calendar"
    assert row["historical_known_at"] == "unknown"
    assert row["parser_qualification"] == "rendered_source_observed_raw_markup_unverified"


def test_heading_gate_ignores_navigation_dates_and_nonpolicy_rows():
    body = html(title="Press conference following the Governing Council meeting of the ECB in Frankfurt")
    body = body.replace("</dl>", "<dt>25/11/2026</dt><dd>Governing Council of the ECB: non-monetary policy meeting (in Frankfurt)</dd></dl>")
    [row] = parse_ecb_calendar("<p>01/01/2026 unrelated date</p>" + body + "<p>01/01/2027 footer</p>", 100)
    assert row["event_kind"] == "press_conference" and row["local_date"] == "2026-10-29"


@pytest.mark.parametrize("body", [
    html().replace(HEADING, "Unrelated calendar"), html("31/02/2026"),
    html("2026-10-29"), html(title="Synthetic unrelated event"),
    html().replace("</dl>", "<dt>29/10/2026</dt></dl>"),
    html().replace("</dl>", f"<dt>29/10/2026</dt><dd>{TITLE}</dd></dl>"),
    "x" * (2 * 1024 * 1024 + 1),
])
def test_official_calendar_drift_invalid_dates_and_unbounded_markup_fail_closed(body):
    with pytest.raises(ValueError):
        parse_ecb_calendar(body, 100)


def test_explicit_time_markup_is_not_qualified_as_a_publication_clock():
    body = html().replace("29/10/2026", '<time datetime="2026-10-29T14:45:00+01:00">29/10/2026</time>')
    [row] = parse_ecb_calendar(body, 100)
    assert row["event_time"] is None and row["publication_time"] is None


def test_calendar_ecb_is_optin_and_raw_bytes_hash_is_exact():
    class Client:
        def __init__(self):
            self.calls = []

        def get_text(self, url):
            self.calls.append(url)
            return ""  # Existing BLS/FOMC default behavior is preserved.

        def get_bytes(self, url):
            self.calls.append(url)
            return b'\xef\xbb\xbf' + html().encode()

    client = Client()
    assert collect_calendars(100, client=client)["events"] == []
    assert ECB_URL not in client.calls
    result = collect_calendars(100, client=client, include_ecb=True)
    [row] = result["events"]
    assert row["raw_sha256"] == hashlib.sha256(b'\xef\xbb\xbf' + html().encode()).hexdigest()
    assert row["hash_scope"] == "response_bytes" and row["data_version"] == "sha256:" + row["raw_sha256"]


def test_calendar_ecb_failure_is_local_to_feed():
    class Client:
        def get_text(self, url):
            return "" if url != ECB_URL else html(title="Changed official wording")

    result = collect_calendars(100, client=Client(), include_ecb=True)
    assert result["errors"][0]["source"] == "ecb"


def test_each_calendar_source_is_known_only_after_its_response():
    clock = {"now": 100}

    class Client:
        def get_text(self, url):
            clock["now"] += 10
            if "bls.gov" in url:
                return ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Consumer Price Index\n"
                        "DTSTART;VALUE=DATE:20261029\nEND:VEVENT\nEND:VCALENDAR")
            return ('<h4>2026 FOMC Meetings</h4><div class="fomc-meeting">'
                    '<div class="fomc-meeting__month">October</div>'
                    '<div class="fomc-meeting__date">27-28</div></div>')

        def get_bytes(self, url):
            clock["now"] += 10
            return html().encode()

    result = collect_calendars(100, client=Client(), include_ecb=True, receipt_clock=lambda: clock["now"])
    assert result["observed_at"] == 130
    assert {row["source"]: row["observed_at"] for row in result["events"]} == {
        "bls": 110, "federalreserve": 120, "ecb": 130,
    }
    assert all(row["observed_at"] == row["materialized_at"] and row["event_time"] is None
               for row in result["events"])


@pytest.mark.parametrize("failure", ["transport", "parser"])
def test_calendar_source_errors_are_timestamped_after_request_finishes(failure):
    clock = {"now": 100.0}

    class Client:
        def get_text(self, url):
            clock["now"] += 10.75
            raise OSError("Synthetic source unavailable")

        def get_bytes(self, url):
            clock["now"] += 10.75
            if failure == "transport":
                raise OSError("Synthetic source unavailable")
            return b"Synthetic markup drift"

    result = collect_calendars(100, client=Client(), include_ecb=True, receipt_clock=lambda: clock["now"])
    assert [error["observed_at"] for error in result["errors"]] == [110, 121, 132]
    assert all(error["observed_at"] == error["materialized_at"] for error in result["errors"])
    assert result["events"] == [] and result["observed_at"] == 132


def test_invalid_receipt_clock_cannot_create_early_calendar_availability():
    class Client:
        def get_text(self, url):
            return html()

    for invalid in (99, True, float("nan"), float("inf")):
        result = collect_calendars(100, client=Client(), include_ecb=True, receipt_clock=lambda invalid=invalid: invalid)
        assert result["events"] == []
        assert all(error["observed_at"] is None for error in result["errors"])
