"""Bounded official calendar parsers; scheduled time is not observed publication."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
_MAX_BYTES = 2 * 1024 * 1024
_MAX_EVENTS = 2000


def _bounded(text: str, observed_at: int):
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("Calendar exceeds the two-megabyte limit.")
    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at < 0:
        raise ValueError("Calendar observation time must be a nonnegative epoch second.")


def _event(source: str, text: str, observed_at: int) -> dict[str, Any]:
    return {
        "source": source,
        "source_url": {"bls": BLS_URL, "federalreserve": FOMC_URL, "ecb": ECB_URL}[source],
        "observed_at": observed_at,
        "data_version": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "status": "planned",
        "publication_time": None,
        "publication_time_verified": False,
        "publication_status": "unknown",
        "historical_known_at": "unknown",
        "event_time": None,
        "event_time_verified": False,
        "original_timezone": "America/New_York",
        "origin": "official_calendar",
        "interpretation": "Schedule only; publication is not confirmed.",
    }


def _ics_value(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def parse_bls_ics(text: str, observed_at: int) -> list[dict[str, Any]]:
    """Parse CPI entries, retaining UID/revisions and Eastern-time DST rules.

    BLS documents Eastern Time for floating schedule times. Explicit foreign
    timezones are not silently relabelled as Eastern. Recurrences are unsupported
    rather than expanded with potentially incorrect exceptions.
    """
    _bounded(text, observed_at)
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    output = []
    blocks = re.findall(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", unfolded, re.S)
    if len(blocks) > _MAX_EVENTS:
        raise ValueError("Calendar exceeds the event-count limit.")
    for block in blocks:
        fields: dict[str, tuple[dict[str, str], str]] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            tokens = key.split(";")
            params = {}
            for token in tokens[1:]:
                if "=" in token:
                    name, item = token.split("=", 1)
                    params[name.upper()] = item.strip('"')
            name = tokens[0].upper()
            if name in fields:
                raise ValueError(f"Duplicate ICS field: {name}.")
            fields[name] = params, _ics_value(value)
        summary = fields.get("SUMMARY", ({}, ""))[1]
        if not re.search(r"consumer price index|\bCPI\b", summary, re.I):
            continue
        event = _event("bls", text, observed_at)
        event.update(
            title=summary,
            event_kind="inflation_release",
            source_event_id=fields.get("UID", ({}, None))[1],
            sequence=fields.get("SEQUENCE", ({}, "0"))[1],
            source_last_modified=fields.get("LAST-MODIFIED", ({}, None))[1],
        )
        if "DTSTART" not in fields:
            raise ValueError("CPI calendar entry is missing DTSTART.")
        params, value = fields["DTSTART"]
        event["original_time"] = value
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            event["local_date"] = datetime.strptime(value, "%Y%m%d").date().isoformat()
            event["time_status"] = "date_only"
        elif "RRULE" in fields or "RECURRENCE-ID" in fields:
            event["time_status"] = "unsupported_recurrence"
        else:
            timezone = "UTC" if value.endswith("Z") else params.get("TZID", "America/New_York")
            if timezone not in {"UTC", "America/New_York", "US/Eastern"}:
                event["original_timezone"] = timezone
                event["time_status"] = "unsupported_timezone"
            else:
                local = datetime.strptime(value.removesuffix("Z"), "%Y%m%dT%H%M%S")
                zone = ZoneInfo(timezone)
                aware = local.replace(tzinfo=zone)
                # Reject ambiguous or nonexistent local wall times instead of choosing a fold.
                round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
                ambiguous = local.replace(tzinfo=zone, fold=1).utcoffset() != aware.utcoffset()
                if round_trip != local or ambiguous:
                    event["time_status"] = "ambiguous_local_time"
                else:
                    event.update(
                        event_time=int(aware.timestamp()),
                        event_time_verified=True,
                        original_timezone=timezone,
                        local_date=local.date().isoformat(),
                        time_status="scheduled_time_verified",
                    )
        if fields.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
            event["status"] = "cancelled"
        output.append(event)
    return output


class _FedHTML(HTMLParser):
    """Collect only the official meeting rows and their nearest year heading."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.year: int | None = None
        self.depth = 0
        self.active: dict | None = None
        self.field: str | None = None
        self.field_depth = 0
        self.rows: list[dict] = []
        self.heading_depth = 0
        self.heading = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {"br", "hr", "img", "input", "meta", "link", "wbr", "source"}:
            self.depth += 1
        classes = attrs.get("class", "").split()
        if tag in {"h2", "h3", "h4"}:
            self.heading_depth, self.heading = self.depth, ""
        if "fomc-meeting" in classes:
            self.active = {"year": self.year, "month": "", "date": "", "time": "", "text": ""}
            self.active["depth"] = self.depth
        if self.active is not None:
            for name in ("month", "date", "time"):
                if f"fomc-meeting__{name}" in classes:
                    self.field, self.field_depth = name, self.depth
            if tag == "time" and attrs.get("datetime"):
                self.active["datetime"] = attrs["datetime"]

    def handle_data(self, data):
        if self.heading_depth:
            self.heading += data
        if self.active is not None:
            self.active["text"] += data + " "
            if self.field:
                self.active[self.field] += data

    def handle_endtag(self, tag):
        if self.heading_depth == self.depth:
            match = re.search(r"\b(20\d\d)\b.*FOMC|FOMC.*\b(20\d\d)\b", self.heading, re.I)
            if match:
                self.year = int(match[1] or match[2])
            self.heading_depth = 0
        if self.field_depth == self.depth:
            self.field = None
        if self.active is not None and self.active["depth"] == self.depth:
            self.rows.append(self.active)
            if len(self.rows) > _MAX_EVENTS:
                raise ValueError("Calendar exceeds the event-count limit.")
            self.active = None
        self.depth = max(0, self.depth - 1)


def parse_fomc_calendar(html: str, observed_at: int) -> list[dict[str, Any]]:
    """Parse official FOMC meeting rows without inventing decision times.

    The FOMC calendar normally supplies dates only. A timed event is returned
    only when the same row supplies an explicit offset-bearing HTML datetime.
    Statement and minutes links do not establish historical publication times.
    """
    _bounded(html, observed_at)
    parser = _FedHTML()
    parser.feed(html)
    output = []
    for row in parser.rows:
        if row["year"] is None:
            continue
        month_name = row["month"].strip()
        day_text = row["date"].strip().replace("–", "-").replace("—", "-")
        # Cross-month dates and emergency annotations remain explicit unsupported records.
        match = re.fullmatch(r"(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*\*?", day_text)
        event = _event("federalreserve", html, observed_at)
        event.update(
            title="FOMC meeting",
            event_kind="central_bank_meeting",
            original_date=day_text,
            original_month=month_name,
            year=row["year"],
            time_status="date_only",
        )
        if match:
            try:
                month = datetime.strptime(month_name, "%B").month
                start = date(row["year"], month, int(match[1]))
                end = date(row["year"], month, int(match[2] or match[1]))
                if end < start:
                    raise ValueError("Reversed meeting dates.")
                event.update(
                    local_date=start.isoformat(),
                    meeting_start_date=start.isoformat(),
                    meeting_end_date=end.isoformat(),
                )
            except ValueError:
                event["time_status"] = "unsupported_date"
        else:
            event["time_status"] = "unsupported_date"
        if row.get("datetime"):
            value = row["datetime"]
            try:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError("Missing timezone.")
                local = instant.astimezone(ZoneInfo("America/New_York"))
                if event.get("meeting_end_date") != local.date().isoformat():
                    raise ValueError("Timestamp date differs from meeting end date.")
                event.update(
                    event_time=int(instant.timestamp()),
                    event_time_verified=True,
                    original_time=value,
                    time_status="scheduled_time_verified",
                )
            except ValueError:
                event["time_status"] = "unsupported_explicit_time"
        event["source_event_id"] = "fomc:" + str(row["year"]) + ":" + month_name + ":" + day_text
        output.append(event)
    return output


def collect_calendars(observed_at: int, *, client: Any) -> dict[str, Any]:
    """Use an injected PublicHTTP-compatible client; failures are per-source.

    No default network client is created. Callers choose whether to fetch.
    """
    result: dict[str, Any] = {"events": [], "errors": [], "observed_at": observed_at}
    for source, url, parser in (
        ("bls", BLS_URL, parse_bls_ics),
        ("federalreserve", FOMC_URL, parse_fomc_calendar),
    ):
        try:
            result["events"].extend(parser(client.get_text(url), observed_at))
        except Exception as exc:
            result["errors"].append(
                {"source": source, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
            )
    return result


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}.")
        result[key] = value
    return result


def parse_ecb_manual_schedule(text: str, observed_at: int) -> list[dict[str, Any]]:
    """Read a sourced manual transcription, never an asserted official ECB feed.

    Schema ``ecb-manual-calendar-v1`` requires the documented official calendar
    URL, a provenance check time/note, and explicit event IDs/revision sequences.
    It accepts date-only meetings or explicit scheduled ISO timestamps. Floating
    timestamps require Europe/Berlin (Frankfurt's IANA zone), Europe/Frankfurt
    as a labelled alias, or UTC. No clock time or actual publication is inferred.
    Unknown event kinds/timezones/ambiguous civil times remain unsupported records.
    """
    _bounded(text, observed_at)
    payload = json.loads(text, object_pairs_hook=_unique_json_object)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "source_url", "provenance", "events"
    }:
        raise ValueError("ECB manual schedule requires schema_version, source_url, provenance and events.")
    if payload["schema_version"] != "ecb-manual-calendar-v1" or payload["source_url"] != ECB_URL:
        raise ValueError("Use ecb-manual-calendar-v1 and the documented official ECB calendar URL.")
    provenance = payload["provenance"]
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"checked_at", "note"}
        or type(provenance["checked_at"]) is not int
        or not 0 <= provenance["checked_at"] <= observed_at
        or not isinstance(provenance["note"], str)
        or not provenance["note"].strip()
    ):
        raise ValueError("Provenance requires a nonfuture checked_at epoch second and a transcription note.")
    rows = payload["events"]
    if not isinstance(rows, list) or len(rows) > _MAX_EVENTS:
        raise ValueError("ECB manual schedule requires at most 2000 events.")
    output, identities = [], set()
    template = _event("ecb", text, observed_at)
    required = {"id", "title", "event_kind", "local_date", "sequence"}
    optional = {"scheduled_time", "timezone", "status", "source_last_modified"}
    for row in rows:
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - optional:
            raise ValueError("Unsupported or missing ECB manual event fields.")
        if any(not isinstance(row[key], str) or not row[key].strip()
               for key in ("id", "title", "event_kind", "local_date")):
            raise ValueError("ECB manual event identity, title, kind and local_date must be nonempty strings.")
        if type(row["sequence"]) is not int or not 0 <= row["sequence"] <= 2147483647:
            raise ValueError("ECB manual sequence must be an integer from zero to 2147483647.")
        identity = row["id"], row["sequence"]
        if identity in identities:
            raise ValueError("Duplicate ECB manual event ID and revision sequence.")
        identities.add(identity)
        local_date = date.fromisoformat(row["local_date"]).isoformat()
        status = row.get("status", "planned")
        if not isinstance(status, str) or status not in {"planned", "cancelled"}:
            raise ValueError("ECB manual status must be planned or cancelled.")
        if "timezone" in row and not isinstance(row["timezone"], str):
            raise ValueError("ECB manual timezone must be a string when supplied.")
        event = dict(template)
        event.update(
            source_url=ECB_URL,
            origin="sourced_manual_schedule",
            source_format="ecb-manual-calendar-v1",
            provenance=dict(provenance),
            event_id="ecb-manual:" + row["id"],
            source_event_id=row["id"],
            title=row["title"],
            event_kind=row["event_kind"],
            local_date=local_date,
            sequence=row["sequence"],
            status=status,
            original_timezone=row.get("timezone"),
            time_status="date_only",
            interpretation="Manual transcription of a sourced schedule; publication is not confirmed.",
        )
        if "source_last_modified" in row:
            if not isinstance(row["source_last_modified"], str):
                raise ValueError("ECB source_last_modified must be an offset-bearing ISO datetime string.")
            modified = datetime.fromisoformat(row["source_last_modified"].replace("Z", "+00:00"))
            if modified.tzinfo is None or modified.timestamp() > observed_at:
                raise ValueError("ECB source_last_modified must have an explicit offset and be nonfuture.")
            event["source_last_modified"] = row["source_last_modified"]
        if "scheduled_time" in row:
            value = row["scheduled_time"]
            if not isinstance(value, str):
                raise ValueError("ECB scheduled_time must be an ISO datetime string.")
            event["original_time"] = value
            try:
                if "T" not in value:
                    raise ValueError("A scheduled_time must contain a clock time.")
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                declared_zone = row.get("timezone")
                if declared_zone not in {None, "UTC", "Europe/Berlin", "Europe/Frankfurt"}:
                    event["time_status"] = "unsupported_timezone"
                elif instant.tzinfo is None and declared_zone is None:
                    event["time_status"] = "unsupported_explicit_time"
                else:
                    zone_name = "Europe/Berlin" if declared_zone == "Europe/Frankfurt" else declared_zone
                    zone = ZoneInfo(zone_name or "Europe/Berlin")
                    if instant.tzinfo is None:
                        aware = instant.replace(tzinfo=zone)
                        round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
                        ambiguous = instant.replace(tzinfo=zone, fold=1).utcoffset() != aware.utcoffset()
                        if round_trip != instant or ambiguous:
                            event["time_status"] = "ambiguous_local_time"
                        else:
                            instant = aware
                    elif declared_zone and instant.utcoffset() != instant.astimezone(zone).utcoffset():
                        raise ValueError("Explicit offset differs from declared timezone.")
                    if event["time_status"] == "date_only":
                        if instant.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat() != local_date:
                            raise ValueError("Scheduled timestamp differs from the Frankfurt event date.")
                        event.update(
                            event_time=int(instant.timestamp()),
                            event_time_verified=True,
                            original_timezone=declared_zone or str(instant.tzinfo),
                            resolved_timezone=zone.key,
                            time_status="scheduled_time_verified",
                        )
            except ValueError:
                event["time_status"] = "unsupported_explicit_time"
        if row["event_kind"] not in {"policy_meeting", "press_conference"}:
            event.update(time_status="unsupported_event_kind", event_time=None, event_time_verified=False)
        output.append(event)
    # Callers may persist rows in order; a lower sequence within this import
    # must not supersede a newer revision merely because of input array order.
    return sorted(output, key=lambda event: (event["source_event_id"], event["sequence"]))
