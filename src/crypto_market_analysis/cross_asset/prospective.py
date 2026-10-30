"""Frozen, native-calendar directional forecasts with locally witnessed receipts.

Artifacts bind declarations, not independently attested preregistration. A score
is a price-only outcome, never evidence of a validated predictive model. Import
history cannot establish prospective performance by declaring itself point-in-time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .identity import resolve_alias
from .prices import number, object_fields, validate_series
from .sessions import expected_observations, validate_calendar
from .storage import CrossAssetStore

_METHOD = "directional-price-only.v1"
_MAX_BYTES = 2 * 1024 * 1024
_TABLE = "cross_asset_assessments"


def _clock(value, name):
    if type(value) is not int or not -62135596800 <= value <= 253402300799:
        raise ValueError(f"{name} must be bounded integer UTC epoch seconds")
    return value


def _canonical(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Protocol requires finite JSON data") from exc
    if len(result.encode()) > _MAX_BYTES:
        raise ValueError("Protocol or assessment exceeds byte budget")
    return result


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def freeze_market_protocol(declaration, *, frozen_at):
    """Return a detached hash-bound declaration frozen before its eligibility.

    ``elapsed.value`` is seconds; ``calendar_days.value`` advances venue-local
    wall time, retaining DST; ``sessions.value`` counts future session closes.
    Deadline snapping uses session closes. Raw price-only is the only supported
    basis; split events crossing the outcome interval invalidate its score.
    """
    _clock(frozen_at, "frozen_at")
    object_fields(declaration, {"schema", "series", "calendar", "method", "horizon", "eligibility", "action_handling"},
                  {"max_receipt_lag_seconds"}, "declaration")
    if declaration["schema"] != "market-protocol.v1" or declaration["method"] != _METHOD:
        raise ValueError("Unsupported market protocol or method")
    descriptor = validate_series({"schema": "market-series.v1", "descriptor": declaration["series"], "bars": []})["descriptor"]
    if descriptor["price_basis"] != "raw" or descriptor["revision_semantics"] != "point_in_time":
        raise ValueError("Prospective protocol requires raw point_in_time observations")
    if descriptor["data_mode"] not in {"live", "demo"}:
        raise ValueError("Imported history cannot establish prospective outcomes")
    if declaration["action_handling"] != "price_only":
        raise ValueError("Only explicitly price_only action handling is supported")
    calendar = validate_calendar(declaration["calendar"])
    if (calendar["id"], calendar["version"]) != (descriptor["calendar_id"], descriptor["calendar_version"]):
        raise ValueError("Protocol calendar does not match series")
    if calendar["known_at"] > frozen_at:
        raise ValueError("Calendar must be known when protocol is frozen")
    horizon = declaration["horizon"]
    object_fields(horizon, {"kind", "value", "deadline_rule"}, set(), "horizon")
    if horizon["kind"] not in {"sessions", "calendar_days", "elapsed"}:
        raise ValueError("Unsupported horizon kind")
    maximum = 3660 * 86400 if horizon["kind"] == "elapsed" else 3660
    if type(horizon["value"]) is not int or not 1 <= horizon["value"] <= maximum:
        raise ValueError("Horizon value must be a bounded positive integer")
    if horizon["deadline_rule"] not in {"exact", "next_session", "previous_session"}:
        raise ValueError("Unsupported horizon deadline rule")
    eligibility = declaration["eligibility"]
    object_fields(eligibility, {"start", "end"}, set(), "eligibility")
    _clock(eligibility["start"], "eligibility.start")
    _clock(eligibility["end"], "eligibility.end")
    if not frozen_at < eligibility["start"] < eligibility["end"]:
        raise ValueError("Protocol must be frozen before its half-open eligibility interval")
    if "max_receipt_lag_seconds" in declaration:
        lag = declaration["max_receipt_lag_seconds"]
        if type(lag) is not int or not 0 <= lag <= 30 * 86400:
            raise ValueError("max_receipt_lag_seconds must be between zero and thirty days")
    payload = {"schema": "frozen-market-protocol.v1", "frozen_at": frozen_at,
               "declaration": json.loads(_canonical({**declaration, "series": descriptor, "calendar": calendar}))}
    return {**payload, "protocol_sha256": _hash(payload)}


def _protocol(artifact):
    object_fields(artifact, {"schema", "frozen_at", "declaration", "protocol_sha256"}, set(), "frozen protocol")
    rebuilt = freeze_market_protocol(artifact["declaration"], frozen_at=artifact["frozen_at"])
    if artifact != rebuilt:
        raise ValueError("Frozen protocol hash or canonical contents mismatch")
    return rebuilt


@contextmanager
def _connection(database, *, write=False):
    path = Path(database)
    if not path.is_file():
        raise ValueError("Authoritative cross-asset database is missing")
    connection = sqlite3.connect(path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    deadline = time.monotonic() + 10
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        yield connection
        if write:
            connection.commit()
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def _stored(database, *, assessment_id=None, idempotency_key=None):
    with _connection(database) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone():
            return None
        if assessment_id is not None:
            row = con.execute(f"SELECT payload FROM {_TABLE} WHERE id=?", (assessment_id,)).fetchone()
        else:
            row = con.execute(f"SELECT payload FROM {_TABLE} WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return json.loads(row[0]) if row else None


def _series(store, descriptor, cutoff):
    series = store.read_series(descriptor["id"], as_of=cutoff, data_mode=descriptor["data_mode"], max_rows=10000)
    if series is None or series["descriptor"] != descriptor:
        raise ValueError("Series lineage differs from frozen protocol or is unavailable")
    evidence = series.get("storage_provenance", {}).get("receipt_evidence")
    if descriptor["data_mode"] == "live" and evidence != "locally_witnessed":
        raise ValueError("Live prospective evidence requires locally witnessed receipts")
    return series


def _target(calendar, descriptor, horizon, issued_at):
    end = min(calendar["supported_end"], issued_at + 3660 * 86400)
    if not calendar["supported_start"] <= issued_at < end:
        raise ValueError("Issuance is outside the supported calendar")
    sessions = expected_observations(calendar, start=issued_at, end=end, as_of=issued_at, interval="session")
    closes = sorted({row["end"] for row in sessions if row["end"] > issued_at})
    if horizon["kind"] == "sessions":
        if len(closes) < horizon["value"]:
            raise ValueError("Calendar does not cover the session horizon")
        deadline = closes[horizon["value"] - 1]
    elif horizon["kind"] == "elapsed":
        deadline = issued_at + horizon["value"]
    else:
        local = datetime.fromtimestamp(issued_at, ZoneInfo(calendar["timezone"]))
        deadline = int((local + timedelta(days=horizon["value"])).timestamp())
    rule = horizon["deadline_rule"]
    if rule == "next_session":
        deadline = next((close for close in closes if close >= deadline), None)
    elif rule == "previous_session":
        deadline = max((close for close in closes if close <= deadline), default=None)
    if deadline is None or not issued_at < deadline <= calendar["supported_end"]:
        raise ValueError("No supported future deadline satisfies the horizon")
    if any(row["start"] < deadline and row["status"] in {"unknown", "halted"} for row in sessions):
        raise ValueError("Horizon schedule is unknown or affected by a halt")
    rows = expected_observations(calendar, start=max(calendar["supported_start"], deadline - 3 * 86400),
                                 end=deadline + 1, as_of=issued_at, interval=descriptor["interval"])
    matches = [row for row in rows if row["end"] == deadline and row["status"] not in {"unknown", "halted"}]
    if len(matches) != 1:
        raise ValueError("Deadline does not match one native observation")
    return matches[0]


def _receipt(series, bar):
    return next((row for row in series.get("storage_provenance", {}).get("bars", [])
                 if row["hash"] == _hash(bar)), None)


def _identity(registry, descriptor, at, cutoff):
    identity = resolve_alias(registry, provider=descriptor["provider"], symbol=descriptor["provider_symbol"],
                             listing_id=descriptor["listing_id"], effective_at=at, as_of=cutoff)
    if (identity["instrument"]["id"] != descriptor["instrument_id"]
            or identity["alias"]["listing_id"] != descriptor["listing_id"]):
        raise ValueError("Resolved identity differs from protocol")
    listing = identity["listing"]
    if listing and (listing["native_currency"] != descriptor["native_currency"]
                    or listing["calendar_id"] != descriptor["calendar_id"]):
        raise ValueError("Resolved listing dimensions differ from protocol")
    return identity


def issue_market_assessment(database, request, *, issued_at):
    """Append an actual-clock probability and its frozen native evidence binding."""
    object_fields(request, {"schema", "protocol", "probability_up", "idempotency_key"}, set(), "assessment request")
    if request["schema"] != "market-assessment-request.v1":
        raise ValueError("Unsupported assessment request schema")
    _clock(issued_at, "issued_at")
    if abs(issued_at - int(time.time())) > 5:
        raise ValueError("Issuance requires the actual current clock")
    probability = number(request["probability_up"], "probability_up", minimum=0)
    if probability > 1:
        raise ValueError("probability_up cannot exceed one")
    key = request["idempotency_key"]
    if not isinstance(key, str) or not key.strip() or len(key) > 256:
        raise ValueError("idempotency_key must be a bounded nonempty string")
    protocol = _protocol(request["protocol"])
    request_hash = _hash(request)
    prior = _stored(database, idempotency_key=key)
    if prior:
        if prior["request_sha256"] != request_hash:
            raise ValueError("Idempotency key already binds a different request")
        return prior
    declaration = protocol["declaration"]
    if not declaration["eligibility"]["start"] <= issued_at < declaration["eligibility"]["end"]:
        raise ValueError("Issuance falls outside frozen protocol eligibility")
    descriptor, calendar = declaration["series"], declaration["calendar"]
    store = CrossAssetStore(database)
    if store.calendar(calendar["id"], calendar["version"], as_of=issued_at) != calendar:
        raise ValueError("Stored calendar differs from frozen protocol or is not yet known")
    registry = store.registry(issued_at)
    if registry is None:
        raise ValueError("No registry is known at issuance")
    identity = _identity(registry, descriptor, issued_at, issued_at)
    if identity["instrument"]["form"] not in {"spot", "share", "fund"}:
        raise ValueError("This prospective method does not support the instrument form")
    series = _series(store, descriptor, issued_at)
    bars = [bar for bar in series["bars"] if bar["end"] <= issued_at and bar["observed_at"] <= issued_at]
    if not bars:
        raise ValueError("No completed native anchor observation is known at issuance")
    anchor = max(bars, key=lambda bar: (bar["end"], bar["observed_at"]))
    _identity(registry, descriptor, anchor["start"], issued_at)
    _identity(registry, descriptor, anchor["end"] - 1, issued_at)
    if anchor["close"] <= 0:
        raise ValueError("Directional price method requires a positive anchor")
    completed = [row for row in expected_observations(calendar, start=anchor["start"], end=issued_at + 1,
                                                     as_of=issued_at, interval=descriptor["interval"])
                 if row["end"] <= issued_at and row["due_at"] is not None and row["due_at"] <= issued_at]
    if (not completed or any(row["status"] in {"unknown", "halted"} for row in completed)
            or max((row["start"], row["end"]) for row in completed) != (anchor["start"], anchor["end"])):
        raise ValueError("Anchor is not the latest completed scheduled native observation")
    anchor_receipt = _receipt(series, anchor)
    lag = declaration.get("max_receipt_lag_seconds")
    if lag is not None and (anchor_receipt is None or anchor_receipt["effective_known_at"] > anchor["end"] + lag):
        raise ValueError("Anchor receipt is late or unwitnessed")
    target = _target(calendar, descriptor, declaration["horizon"], issued_at)
    payload = {"schema": "market-assessment.v1", "issued_at": issued_at, "idempotency_key": key,
               "protocol": protocol, "request_sha256": request_hash, "probability_up": probability,
               "anchor": anchor, "anchor_receipt": anchor_receipt, "target": target,
               "evidence_scope": "synthetic_demo" if descriptor["data_mode"] == "demo" else "locally_witnessed_live",
               "limitations": "Price-only directional outcome; declaration is not independent attestation or model validation."}
    payload["assessment_id"] = _hash(payload)
    encoded = _canonical(payload)
    with _connection(database, write=True) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
        for operation in ("UPDATE", "DELETE"):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {_TABLE}_no_{operation.lower()} BEFORE {operation} ON {_TABLE} "
                        "BEGIN SELECT RAISE(ABORT, 'append-only assessment'); END")
        existing = con.execute(f"SELECT payload FROM {_TABLE} WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            previous = json.loads(existing[0])
            if previous["request_sha256"] != request_hash:
                raise ValueError("Idempotency key already binds a different request")
            return previous
        con.execute(f"INSERT INTO {_TABLE} VALUES (?,?,?)", (payload["assessment_id"], key, encoded))
    return payload


def evaluate_market_assessment(database, request, *, as_of):
    """Read one fixed target, excluding late receipts, splits and unmatched lineage."""
    object_fields(request, {"assessment_id"}, set(), "evaluation request")
    if not isinstance(request["assessment_id"], str) or len(request["assessment_id"]) != 64:
        raise ValueError("assessment_id must be a SHA256 identifier")
    _clock(as_of, "as_of")
    assessment = _stored(database, assessment_id=request["assessment_id"])
    if assessment is None:
        raise ValueError("Assessment does not exist")
    if as_of < assessment["issued_at"]:
        raise ValueError("Evaluation cutoff precedes issuance")
    declaration = _protocol(assessment["protocol"])["declaration"]
    result = {"schema": "market-assessment-outcome.v1", "assessment_id": assessment["assessment_id"],
              "as_of": as_of, "status": "pending", "brier_score": None, "evidence_scope": assessment["evidence_scope"],
              "action_handling": "price_only", "limitations": assessment["limitations"]}
    target = assessment["target"]
    if as_of < target["due_at"]:
        return result
    try:
        store = CrossAssetStore(database)
        series = _series(store, declaration["series"], as_of)
        registry = store.registry(as_of)
        if registry is None:
            raise ValueError("Registry unavailable at outcome cutoff")
        _identity(registry, declaration["series"], target["start"], as_of)
        _identity(registry, declaration["series"], target["end"] - 1, as_of)
    except ValueError as exc:
        return {**result, "status": "excluded", "reason": str(exc)}
    if any(assessment["anchor"]["end"] <= action["effective_at"] <= target["end"] and action["kind"] == "split"
           for action in series["actions"]):
        return {**result, "status": "excluded", "reason": "split crosses raw price-only outcome"}
    bars = [bar for bar in series["bars"] if (bar["start"], bar["end"]) == (target["start"], target["end"])
            and bar["observed_at"] <= as_of]
    if not bars:
        return {**result, "status": "missing"}
    bar = max(bars, key=lambda row: row["observed_at"])
    receipt = _receipt(series, bar)
    lag = declaration.get("max_receipt_lag_seconds")
    if lag is not None and (receipt is None or receipt["effective_known_at"] > target["due_at"] + lag):
        timely = _series(store, declaration["series"], min(as_of, target["due_at"] + lag))
        candidates = [row for row in timely["bars"]
                      if (row["start"], row["end"]) == (target["start"], target["end"])]
        if not candidates:
            return {**result, "status": "excluded", "reason": "target receipt is late or unwitnessed"}
        bar = max(candidates, key=lambda row: row["observed_at"])
        receipt = _receipt(timely, bar)
    if bar["close"] <= 0:
        return {**result, "status": "excluded", "reason": "nonpositive target is unsupported"}
    outcome = int(bar["close"] > assessment["anchor"]["close"])
    return {**result, "status": "scored", "outcome_up": outcome,
            "brier_score": (assessment["probability_up"] - outcome) ** 2,
            "anchor": assessment["anchor"], "target": bar, "target_receipt": receipt,
            "price_return": bar["close"] / assessment["anchor"]["close"] - 1}
