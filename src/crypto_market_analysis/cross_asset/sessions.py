"""Versioned native-session and publication grids, independent of legacy hours.

All clocks are integer UTC epoch seconds. Query windows and supported bounds are
half open. A supplied explicit schedule asserts that omitted sessions are closed
*only* within those bounds. Its source/known-at claim is the importer's evidence;
the validator cannot establish that an external source was actually archived.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from importlib.metadata import version
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_ROWS = 10_000
_MAX_QUERY_SECONDS = 3660 * 86400
_MIN_EPOCH = -62_135_596_800
_MAX_EPOCH = 253_402_300_799


def _clock(value, name):
    if type(value) is not int or not _MIN_EPOCH <= value <= _MAX_EPOCH:
        raise ValueError(f"{name} must be a bounded integer epoch second.")
    return value


def _text(value, name, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters.")
    return value


def _source(value):
    _text(value, "source_url", 2048)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source_url must be an HTTPS URL without credentials.")
    return value


def _fields(value, required, optional=(), name="document"):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError(f"{name} has missing or unsupported fields.")


def _day(value):
    if not isinstance(value, str):
        raise ValueError("Session dates must be ISO YYYY-MM-DD strings.")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Session dates must be ISO YYYY-MM-DD strings.") from exc
    if result.isoformat() != value:
        raise ValueError("Session dates must be ISO YYYY-MM-DD strings.")
    return result


def _bounds(start, end):
    _clock(start, "start")
    _clock(end, "end")
    if end <= start:
        raise ValueError("end must be greater than start.")


def _local(timestamp, zone):
    try:
        return datetime.fromtimestamp(timestamp, zone)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("Timestamp cannot be represented in the calendar's local time zone.") from exc


def validate_calendar(document) -> dict:
    """Return a detached canonical JSON calendar or reject an unsupported claim.

    ``profile='continuous'`` defines local-midnight sessions on every day. Use UTC
    for 24/7 cryptocurrency series. Explicit profiles retain supplied UTC segment
    bounds, early closes and breaks; optional publication_at overrides close as
    the base due time. provider_delay_seconds is added to that base due time.
    Unexpected halts require separately sourced, known-at records.
    """
    _fields(document, ("schema", "id", "version", "timezone", "source_url", "known_at",
                       "supported_start", "supported_end", "profile", "sessions"),
            ("provider_delay_seconds", "halts", "generation"))
    result = deepcopy(document)
    if result["schema"] != "market-calendar.v1":
        raise ValueError("Unsupported calendar schema.")
    for name in ("id", "version", "timezone"):
        _text(result[name], name)
    try:
        zone = ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must identify an installed IANA time zone.") from exc
    _source(result["source_url"])
    _clock(result["known_at"], "known_at")
    low, high = result["supported_start"], result["supported_end"]
    _bounds(low, high)
    if high - low > 40 * 366 * 86400:
        raise ValueError("Calendar supported range exceeds forty years.")
    if result["profile"] not in {"explicit", "continuous"}:
        raise ValueError("Unsupported calendar profile.")
    delay = result.setdefault("provider_delay_seconds", 0)
    if type(delay) is not int or not 0 <= delay <= 30 * 86400:
        raise ValueError("provider_delay_seconds must be between zero and thirty days.")
    sessions = result["sessions"]
    if not isinstance(sessions, list) or len(sessions) > _MAX_ROWS:
        raise ValueError("Calendar sessions must be a list of at most 10000 entries.")
    if result["profile"] == "continuous" and sessions:
        raise ValueError("Continuous calendars cannot also contain explicit sessions.")
    seen, spans = set(), []
    for session in sessions:
        _fields(session, ("date", "segments"), ("publication_at",), "session")
        day = _day(session["date"])
        if session["date"] in seen:
            raise ValueError("Duplicate session date.")
        seen.add(session["date"])
        segments = session["segments"]
        if not isinstance(segments, list) or not 0 <= len(segments) <= 8:
            raise ValueError("Each session permits at most eight segments.")
        if not segments and "publication_at" not in session:
            raise ValueError("An explicit session needs segments or a publication time.")
        previous = None
        for segment in segments:
            _fields(segment, ("start", "end"), name="segment")
            start, end = segment["start"], segment["end"]
            _bounds(start, end)
            if end - start > 2 * 86400:
                raise ValueError("A trading segment cannot exceed two elapsed days.")
            if start < low or end > high or (previous is not None and start < previous):
                raise ValueError("Segments must be ordered, nonoverlapping and inside supported bounds.")
            if not (_local(start, zone).date() <= day <= _local(end - 1, zone).date()):
                raise ValueError("Session date must fall within its segment's local dates.")
            previous = end
            spans.append((start, end))
        if "publication_at" in session:
            publication = _clock(session["publication_at"], "publication_at")
            if not segments and not low <= publication < high:
                raise ValueError("A publication-only event must lie inside supported bounds.")
            if segments and publication < segments[-1]["end"]:
                raise ValueError("Publication cannot precede the observation interval's close.")
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:], strict=False)):
        raise ValueError("Sessions overlap.")
    result["sessions"] = sorted(sessions, key=lambda item: item["date"])
    halts = result.setdefault("halts", [])
    if not isinstance(halts, list) or len(halts) > _MAX_ROWS:
        raise ValueError("halts must be a list of at most 10000 events.")
    for halt in halts:
        _fields(halt, ("start", "end", "known_at", "source_url"), name="halt")
        _bounds(halt["start"], halt["end"])
        _clock(halt["known_at"], "halt known_at")
        _source(halt["source_url"])
        if halt["start"] < low or halt["end"] > high:
            raise ValueError("Halt lies outside supported calendar bounds.")
    result["halts"] = sorted(halts, key=lambda item: (item["start"], item["known_at"]))
    if "generation" in result:
        generation = result["generation"]
        _fields(generation, ("library", "library_version", "mic", "historical_known_at_guaranteed", "note"),
                name="generation")
        for name in ("library", "library_version", "mic", "note"):
            _text(generation[name], name, 2048)
        if generation["historical_known_at_guaranteed"] is not False:
            raise ValueError("Generated calendar rules do not guarantee historical known-at availability.")
    return result


def _query(calendar, start, end, as_of, interval):
    calendar = validate_calendar(calendar)
    _bounds(start, end)
    _clock(as_of, "as_of")
    if end - start > _MAX_QUERY_SECONDS:
        raise ValueError("Query window exceeds ten years.")
    if interval not in {"session", "1h", "reference"}:
        raise ValueError("interval must be session, 1h or reference.")
    return calendar


def _sessions(calendar, start, end):
    if calendar["profile"] == "explicit":
        yield from calendar["sessions"]
        return
    zone = ZoneInfo(calendar["timezone"])
    low, high = max(start, calendar["supported_start"]), min(end, calendar["supported_end"])
    if low >= high:
        return
    day = _local(low, zone).date()
    last = _local(high - 1, zone).date()
    if last == date.max:
        raise ValueError("A continuous calendar needs a representable next local midnight.")
    while day <= last:
        opening = int(datetime.combine(day, time(), zone).timestamp())
        closing = int(datetime.combine(day + timedelta(days=1), time(), zone).timestamp())
        # Partial supported days are unknown, not fabricated shortened sessions.
        if opening >= calendar["supported_start"] and closing <= calendar["supported_end"]:
            yield {"date": day.isoformat(), "segments": [{"start": opening, "end": closing}]}
        day += timedelta(days=1)


def _row(calendar, session_date, start, end, due_at, status, segments=None):
    row = {"calendar_id": calendar["id"], "calendar_version": calendar["version"],
           "session_date": session_date, "start": start, "end": end, "due_at": due_at, "status": status}
    if segments is not None:
        row["segments"] = deepcopy(segments)
    return row


def _unknown(calendar, start, end, as_of):
    if calendar["known_at"] > as_of:
        return [_row(calendar, None, start, end, None, "unknown")]
    result = []
    low, high = calendar["supported_start"], calendar["supported_end"]
    if start < low:
        result.append(_row(calendar, None, start, min(low, end), None, "unknown"))
    if end > high:
        result.append(_row(calendar, None, max(start, high), end, None, "unknown"))
    if calendar["profile"] == "continuous":
        zone = ZoneInfo(calendar["timezone"])
        for bound, side in ((low, "start"), (high, "end")):
            local = _local(bound, zone)
            if local.time() != time():
                midnight = datetime.combine(local.date(), time(), zone)
                left = bound if side == "start" else int(midnight.timestamp())
                if side == "start" and local.date() == date.max:
                    raise ValueError("A continuous calendar needs a representable next local midnight.")
                right = int((midnight + timedelta(days=1)).timestamp()) if side == "start" else bound
                left, right = max(start, left), min(end, right)
                if left < right:
                    result.append(_row(calendar, None, left, right, None, "unknown"))
    merged = []
    for row in sorted(result, key=lambda item: item["start"]):
        if merged and row["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], row["end"])
        else:
            merged.append(row)
    return merged


def expected_observations(calendar, *, start, end, as_of, interval="session") -> list[dict]:
    """Return native intervals intersecting the window, retaining full bar bounds.

    Session bars retain their trading segments. Hourly bars start at each segment
    opening and truncate at its close; no bar straddles a lunch break. Reference
    rows require a declared publication_at and preserve its observation interval.
    Unknown rows explicitly mark unsupported or not-yet-known schedule windows.
    """
    calendar = _query(calendar, start, end, as_of, interval)
    rows = _unknown(calendar, start, end, as_of)
    if calendar["known_at"] > as_of:
        return rows
    visible_halts = [halt for halt in calendar["halts"] if halt["known_at"] <= as_of]
    for session in _sessions(calendar, start, end):
        segments = session["segments"]
        if interval == "reference" and "publication_at" not in session:
            raise ValueError("reference interval requires explicit publication_at on every session.")
        if not segments:
            if interval != "reference":
                raise ValueError("Publication-only calendars require the reference interval.")
            spans = [(session["publication_at"], session["publication_at"])]
        elif interval in {"session", "reference"}:
            spans = [(segments[0]["start"], segments[-1]["end"])]
        else:
            spans = []
            for segment in segments:
                for opening in range(segment["start"], segment["end"], 3600):
                    spans.append((opening, min(opening + 3600, segment["end"])))
        for opening, closing in spans:
            if not (start <= opening < end if opening == closing else opening < end and closing > start):
                continue
            due = session.get("publication_at", closing) + calendar["provider_delay_seconds"]
            status = "expected" if due <= as_of else "not_yet_due"
            # Any interruption makes the native bar explicitly affected; it is not
            # silently reclassified as a scheduled exchange closure.
            trading_spans = segments if interval == "session" else [{"start": opening, "end": closing}]
            if any(halt["start"] < span["end"] and halt["end"] > span["start"]
                   for halt in visible_halts for span in trading_spans):
                status = "halted"
            rows.append(_row(calendar, session["date"], opening, closing, due, status,
                             segments if interval == "session" else None))
            if len(rows) > _MAX_ROWS:
                raise ValueError("Query exceeds 10000 expected observations.")
    return sorted(rows, key=lambda row: (row["start"], row["end"]))


def _closed_intervals(calendar, start, end, as_of):
    if calendar["known_at"] > as_of:
        return []
    low, high = max(start, calendar["supported_start"]), min(end, calendar["supported_end"])
    if low >= high:
        return []
    spans = [(segment["start"], segment["end"])
             for session in _sessions(calendar, start, end) for segment in session["segments"]
             if segment["start"] < high and segment["end"] > low]
    spans += [(row["start"], row["end"]) for row in _unknown(calendar, start, end, as_of)]
    result, cursor = [], low
    for opening, closing in sorted(spans):
        opening, closing = max(low, opening), min(high, closing)
        if opening > cursor:
            result.append({"start": cursor, "end": opening, "status": "closed"})
        cursor = max(cursor, closing)
    if cursor < high:
        result.append({"start": cursor, "end": high, "status": "closed"})
    return result


def coverage_report(calendar, bars, *, start, end, as_of, interval="session") -> dict:
    """Compare expected native bounds to receipts visible at the cutoff.

    Bars use start/end/observed_at; optional status='stale' records provider
    evidence. A receipt before interval completion is also stale. A newer visible
    receipt wins; future receipts cannot repair historical coverage. Calendar
    closure, unknown schedule, provider delay and declared halt stay distinct.
    """
    calendar = _query(calendar, start, end, as_of, interval)
    rows = expected_observations(calendar, start=start, end=end, as_of=as_of, interval=interval)
    if not isinstance(bars, list) or len(bars) > 100_000:
        raise ValueError("bars must be a list of at most 100000 records.")
    visible = {}
    for bar in bars:
        if not isinstance(bar, dict) or not {"start", "end", "observed_at"} <= bar.keys():
            raise ValueError("Each bar requires start, end and observed_at.")
        opening, closing = _clock(bar["start"], "bar start"), _clock(bar["end"], "bar end")
        if closing < opening:
            raise ValueError("Bar end cannot precede its start.")
        received = _clock(bar["observed_at"], "bar observed_at")
        if bar.get("status", "observed") not in {"observed", "stale"}:
            raise ValueError("Bar status must be observed or stale.")
        if received > as_of:
            continue
        key = opening, closing
        prior = visible.get(key)
        if prior is None or received > prior["observed_at"]:
            visible[key] = bar
        elif received == prior["observed_at"] and bar.get("status", "observed") != prior.get("status", "observed"):
            raise ValueError("Conflicting bar statuses at the same receipt clock.")
    for row in rows:
        bar = visible.get((row["start"], row["end"]))
        if row["status"] in {"unknown", "halted"}:
            continue
        if bar is not None:
            row["observed_at"] = bar["observed_at"]
            row["status"] = "stale" if bar.get("status") == "stale" or bar["observed_at"] < row["end"] else "observed"
        elif row["status"] == "expected":
            row["status"] = "missing"
    counts = {status: 0 for status in ("observed", "missing", "stale", "not_yet_due", "halted", "unknown")}
    counts.update(Counter(row["status"] for row in rows))
    closed = _closed_intervals(calendar, start, end, as_of)
    return {"schema": "market-coverage.v1", "calendar_id": calendar["id"],
            "calendar_version": calendar["version"], "calendar_known_at": calendar["known_at"],
            "start": start, "end": end, "as_of": as_of, "interval": interval,
            "counts": counts, "observations": rows, "closed_intervals": closed,
            "closed_seconds": sum(row["end"] - row["start"] for row in closed)}


def generate_exchange_calendar(mic, *, start_date, end_date, known_at) -> dict:
    """Generate XNYS/XPAR/XTKS rules with pinned library provenance, offline.

    Dates are inclusive venue-local dates. This is a current rules projection,
    not an official historical schedule archive or historical knowledge proof.
    Callers must retain the generated artifact and its actual receipt clock.
    """
    if mic not in {"XNYS", "XPAR", "XTKS"}:
        raise ValueError("Supported generated exchange MICs are XNYS, XPAR and XTKS.")
    first, last = _day(start_date), _day(end_date)
    if last < first or (last - first).days > 3660:
        raise ValueError("Generated schedule range must be ordered and at most ten years.")
    if first.year < 2 or last.year > 9998:
        raise ValueError("Generated schedule dates need representable surrounding dates.")
    _clock(known_at, "known_at")
    import exchange_calendars as xcals
    import pandas as pd

    sources = {"XNYS": "https://www.nyse.com/markets/hours-calendars",
               "XPAR": "https://www.euronext.com/en/trade/trading-hours-holidays",
               "XTKS": "https://www.jpx.co.jp/english/corporate/about-jpx/calendar/index.html"}
    exchange = xcals.get_calendar(mic, start=(first - timedelta(days=7)).isoformat(),
                                  end=(last + timedelta(days=7)).isoformat())
    schedule = exchange.schedule.loc[start_date:end_date]
    sessions = []
    for label, row in schedule.iterrows():
        opening, closing = int(row["open"].timestamp()), int(row["close"].timestamp())
        if pd.notna(row["break_start"]):
            segments = [{"start": opening, "end": int(row["break_start"].timestamp())},
                        {"start": int(row["break_end"].timestamp()), "end": closing}]
        else:
            segments = [{"start": opening, "end": closing}]
        sessions.append({"date": label.date().isoformat(), "segments": segments})
    zone = ZoneInfo(str(exchange.tz))
    library_version = version("exchange-calendars")
    return validate_calendar({"schema": "market-calendar.v1", "id": mic,
                              "version": f"exchange-calendars-{library_version}:{start_date}:{end_date}",
                              "timezone": str(exchange.tz), "source_url": sources[mic], "known_at": known_at,
                              "supported_start": int(datetime.combine(first, time(), zone).timestamp()),
                              "supported_end": int(datetime.combine(last + timedelta(days=1), time(), zone).timestamp()),
                              "profile": "explicit", "sessions": sessions,
                              "generation": {"library": "exchange-calendars", "library_version": library_version,
                                             "mic": mic, "historical_known_at_guaranteed": False,
                                             "note": "Current library rules derived from exchange schedules; not an official historical known-at archive."}})
