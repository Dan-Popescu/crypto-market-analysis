"""Native-price contracts and receipt-time selection for cross-asset research.

Validation never turns a publisher's currently adjusted history into historical
point-in-time evidence. A vintage is identified by its actual receipt clock.
"""

from __future__ import annotations

import copy
import math
import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from ..evidence import content_hash

SCHEMA = "market-series.v1"
BASES = {"raw", "split_adjusted", "total_return", "continuous"}
ACTION_KINDS = {"split", "cash_dividend"}
MAX_BAR_VINTAGES = 100_000
MAX_ACTION_VINTAGES = 1_000


def object_fields(value: Any, required: set[str], optional: set[str], name: str) -> dict:
    """Reject typos and coercions at the JSON boundary."""
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be an object")
    if missing := required - value.keys():
        raise ValueError(f"{name} missing fields: {', '.join(sorted(missing))}")
    if unknown := value.keys() - required - optional:
        raise ValueError(f"{name} unknown fields: {', '.join(sorted(unknown))}")
    return value


def timestamp(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be integer epoch seconds")
    if not -62135596800 <= value <= 253402300799:
        raise ValueError(f"{name} must be in the representable UTC epoch range")
    return value


def number(value: Any, name: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


def text_field(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a nonempty trimmed string")
    return value


def currency(value: Any, name: str) -> str:
    # Includes cryptocurrency quote codes. Code existence belongs to identity
    # resolution; this local contract does not pretend to be an ISO registry.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", value):
        raise ValueError(f"{name} must be an uppercase currency code")
    return value


def source_url(value: Any, name: str) -> str:
    result = text_field(value, name)
    parsed = urlparse(result)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an HTTP(S) source URL without credentials")
    return result


def _coverage(value: Any) -> dict:
    value = object_fields(value, {"start", "end", "kinds", "status", "known_at", "source_url"}, set(), "action_coverage")
    timestamp(value["start"], "action_coverage.start")
    timestamp(value["end"], "action_coverage.end")
    timestamp(value["known_at"], "action_coverage.known_at")
    if value["end"] <= value["start"]:
        raise ValueError("action_coverage.end must follow start")
    if not isinstance(value["status"], str) or value["status"] not in {"declared_complete", "unknown"}:
        raise ValueError("invalid action_coverage.status")
    kinds = value["kinds"]
    if not isinstance(kinds, list) or any(not isinstance(k, str) or k not in ACTION_KINDS for k in kinds):
        raise ValueError("action_coverage.kinds must list supported action kinds")
    if len(set(kinds)) != len(kinds):
        raise ValueError("action_coverage.kinds must be unique")
    source_url(value["source_url"], "action_coverage.source_url")
    return {**value, "kinds": sorted(kinds)}


def _descriptor(value: Any) -> dict:
    required = {
        "id", "instrument_id", "listing_id", "provider", "provider_symbol", "native_currency", "unit",
        "price_basis", "data_mode", "calendar_id", "calendar_version", "interval", "revision_semantics",
    }
    value = object_fields(value, required, {"source_url", "rights", "action_coverage"}, "descriptor")
    for key in required - {"listing_id"}:
        text_field(value[key], f"descriptor.{key}")
    if value["listing_id"] is not None:
        text_field(value["listing_id"], "descriptor.listing_id")
    currency(value["native_currency"], "descriptor.native_currency")
    for field, options in (
        ("price_basis", BASES), ("data_mode", {"live", "import", "demo"}),
        ("interval", {"session", "1h", "reference"}),
        ("revision_semantics", {"captured_history", "point_in_time"}),
    ):
        if value[field] not in options:
            raise ValueError(f"invalid descriptor.{field}")
    if "source_url" in value:
        source_url(value["source_url"], "descriptor.source_url")
    if "rights" in value:
        text_field(value["rights"], "descriptor.rights")
    result = copy.deepcopy(value)
    if "action_coverage" in value:
        result["action_coverage"] = _coverage(value["action_coverage"])
    return result


def _bar(value: Any, *, allow_reference_point: bool = False) -> dict:
    value = object_fields(value, {"start", "end", "open", "high", "low", "close", "observed_at"}, {"volume", "session_date"}, "bar")
    result = dict(value)
    for key in ("start", "end", "observed_at"):
        timestamp(value[key], f"bar.{key}")
    if value["end"] < value["start"] or (value["end"] == value["start"] and not allow_reference_point):
        raise ValueError("bar.end must follow start")
    if value["observed_at"] < value["end"]:
        raise ValueError("closed bar cannot be observed before its end")
    for key in ("open", "high", "low", "close"):
        result[key] = number(value[key], f"bar.{key}")
    if result["high"] < max(result["open"], result["close"], result["low"]):
        raise ValueError("bar.high violates OHLC ordering")
    if result["low"] > min(result["open"], result["close"], result["high"]):
        raise ValueError("bar.low violates OHLC ordering")
    if "volume" in value:
        result["volume"] = number(value["volume"], "bar.volume", minimum=0)
    if "session_date" in value:
        text_field(value["session_date"], "bar.session_date")
        try:
            if date.fromisoformat(value["session_date"]).isoformat() != value["session_date"]:
                raise ValueError
        except ValueError:
            raise ValueError("bar.session_date must be YYYY-MM-DD") from None
    return result


def _action(value: Any) -> dict:
    value = object_fields(
        value, {"id", "kind", "effective_at", "announced_at", "observed_at"},
        {"ratio", "amount", "currency", "pay_at"}, "action",
    )
    text_field(value["id"], "action.id")
    if not isinstance(value["kind"], str) or value["kind"] not in ACTION_KINDS:
        raise ValueError("invalid action.kind")
    for key in ("effective_at", "observed_at"):
        timestamp(value[key], f"action.{key}")
    if value["announced_at"] is not None:
        timestamp(value["announced_at"], "action.announced_at")
        if value["announced_at"] > value["observed_at"]:
            raise ValueError("action announcement cannot follow receipt")
    result = dict(value)
    if value["kind"] == "split":
        if "ratio" not in value or {"amount", "currency", "pay_at"} & value.keys():
            raise ValueError("split requires only a ratio, without dividend fields")
        result["ratio"] = number(value["ratio"], "action.ratio", positive=True)
    else:
        if "amount" not in value or "ratio" in value:
            raise ValueError("cash_dividend requires amount, without ratio")
        result["amount"] = number(value["amount"], "action.amount", minimum=0)
        if "currency" in value:
            currency(value["currency"], "action.currency")
        if "pay_at" in value:
            timestamp(value["pay_at"], "action.pay_at")
            if value["pay_at"] < value["effective_at"]:
                raise ValueError("dividend payment cannot precede entitlement")
    return result


def _vintages(rows: list, validator: Any, keys: tuple[str, ...], name: str) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError(f"{name} must be an array")
    unique: dict[tuple, dict] = {}
    for raw in rows:
        row = validator(raw)
        key = tuple(row[k] for k in keys)
        if key in unique and unique[key] != row:
            raise ValueError(f"conflicting {name} at the same receipt vintage")
        unique[key] = row
    return sorted(unique.values(), key=lambda row: tuple(row[k] for k in keys))


def _storage_provenance(value: Any, bars: list[dict], actions: list[dict]) -> dict:
    value = object_fields(
        value, {"descriptor_hash", "registry_hash", "receipt_evidence", "bars", "actions"},
        set(), "storage_provenance",
    )
    for field in ("descriptor_hash", "registry_hash"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise ValueError(f"storage_provenance.{field} must be a SHA-256 hash")
    if value["receipt_evidence"] not in ("locally_witnessed", "self_declared_history"):
        raise ValueError("invalid storage_provenance.receipt_evidence")
    for field, rows in (("bars", bars), ("actions", actions)):
        entries = value[field]
        if not isinstance(entries, list):
            raise ValueError(f"storage_provenance.{field} must be an array")
        hashes: set[str] = set()
        for entry in entries:
            object_fields(entry, {"hash", "declared_observed_at", "received_at", "effective_known_at"}, set(), "receipt provenance")
            if not isinstance(entry["hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["hash"]):
                raise ValueError("receipt provenance.hash must be a SHA-256 hash")
            if entry["hash"] in hashes:
                raise ValueError("receipt provenance hashes must be unique")
            hashes.add(entry["hash"])
            for clock in ("declared_observed_at", "received_at", "effective_known_at"):
                timestamp(entry[clock], f"receipt provenance.{clock}")
            minimum = entry["declared_observed_at"]
            if value["receipt_evidence"] == "locally_witnessed":
                minimum = max(minimum, entry["received_at"])
            if entry["effective_known_at"] < minimum:
                raise ValueError("effective_known_at cannot precede the applicable receipt/declaration clock")
        if hashes != {content_hash(row) for row in rows}:
            raise ValueError(f"storage_provenance.{field} must exactly identify the returned original rows")
        by_hash = {entry["hash"]: entry for entry in entries}
        for row in rows:
            if by_hash[content_hash(row)]["declared_observed_at"] != row["observed_at"]:
                raise ValueError("receipt provenance declared clock does not match original row")
    return copy.deepcopy(value)


def validate_series(document: Any) -> dict:
    """Validate one basis, quote, unit and calendar; canonicalize receipt vintages.

    Identical duplicate vintages collapse; conflicting duplicates fail. Input order
    has no significance. Different native intervals must not overlap. Instrument,
    listing and calendar references must additionally be resolved by ingestion.
    """
    document = object_fields(document, {"schema", "descriptor", "bars"}, {"actions", "storage_provenance"}, "series")
    if document["schema"] != SCHEMA:
        raise ValueError(f"series.schema must be {SCHEMA}")
    descriptor = _descriptor(document["descriptor"])
    for field, limit in (("bars", MAX_BAR_VINTAGES), ("actions", MAX_ACTION_VINTAGES)):
        if isinstance(document.get(field), list) and len(document[field]) > limit:
            raise ValueError(f"series.{field} exceeds the bounded {limit} vintage limit")
    bars = _vintages(
        document["bars"], lambda row: _bar(row, allow_reference_point=descriptor["interval"] == "reference"),
        ("start", "end", "observed_at"), "bars",
    )
    intervals = sorted({(b["start"], b["end"]) for b in bars})
    if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:], strict=False)):
        raise ValueError("distinct native bar intervals overlap")
    actions = _vintages(document.get("actions", []), _action, ("id", "observed_at"), "actions")
    kinds: dict[str, str] = {}
    for action in actions:
        if action["id"] in kinds and kinds[action["id"]] != action["kind"]:
            raise ValueError("an action id cannot change kind across vintages")
        kinds[action["id"]] = action["kind"]
    result = {"schema": SCHEMA, "descriptor": descriptor, "bars": bars, "actions": actions}
    if "storage_provenance" in document:
        if not isinstance(document["storage_provenance"], dict):
            raise ValueError("storage_provenance must be an object")
        if document["storage_provenance"].get("descriptor_hash") != content_hash(document["descriptor"]):
            raise ValueError("storage_provenance.descriptor_hash does not match original descriptor")
        # Hashes identify the original JSON numerical representations; do not
        # silently turn integer source values into different evidence hashes.
        originals = {
            field: {content_hash(row): row for row in document.get(field, [])}
            for field in ("bars", "actions")
        }
        original_bars = list(originals["bars"].values())
        original_actions = list(originals["actions"].values())
        result["storage_provenance"] = _storage_provenance(document["storage_provenance"], original_bars, original_actions)
        # Preserve exact hashed values once storage has supplied receipt evidence.
        result["bars"] = sorted(original_bars, key=lambda b: (b["start"], b["end"], b["observed_at"]))
        result["actions"] = sorted(original_actions, key=lambda a: (a["id"], a["observed_at"]))
        result["descriptor"] = copy.deepcopy(document["descriptor"])
    return result


def receipt_clocks(series: dict, field: str) -> dict[str, int]:
    return {entry["hash"]: entry["effective_known_at"] for entry in series.get("storage_provenance", {}).get(field, [])}


def effective_known_at(series: dict, row: dict, field: str, *, receipt_index: dict | None = None) -> int:
    provenance = series.get("storage_provenance")
    if provenance is None:
        return row["observed_at"]
    receipt_index = receipt_clocks(series, field) if receipt_index is None else receipt_index
    if (known_at := receipt_index.get(content_hash(row))) is None:
        raise ValueError("row lacks storage receipt provenance")
    return max(row["observed_at"], known_at)


def bars_as_of(series: Any, as_of: int, *, start: int | None = None, end: int | None = None) -> list[dict]:
    """Latest receipt per closed native interval entirely within [start, end].

    Receipt-version order is (effective_known_at, declared observed_at), matching
    persisted snapshot selection. A newly received revision can carry an older
    publisher clock; its actual known clock still determines version precedence.
    """
    return _bars_as_of_validated(validate_series(series), as_of, start=start, end=end)


def _bars_as_of_validated(series: dict, as_of: int, *, start: int | None = None, end: int | None = None) -> list[dict]:
    timestamp(as_of, "as_of")
    if start is not None:
        timestamp(start, "start")
    if end is not None:
        timestamp(end, "end")
    if start is not None and end is not None and start >= end:
        raise ValueError("end must follow start")
    selected: dict[tuple, dict] = {}
    ranks: dict[tuple, tuple[int, int]] = {}
    clocks = receipt_clocks(series, "bars")
    for bar in series["bars"]:
        known_at = effective_known_at(series, bar, "bars", receipt_index=clocks)
        if known_at > as_of or bar["end"] > as_of:
            continue
        if start is not None and bar["start"] < start:
            continue
        if end is not None and bar["end"] > end:
            continue
        key = (bar["start"], bar["end"])
        rank = (known_at, bar["observed_at"])
        if key not in ranks or rank > ranks[key]:
            ranks[key] = rank
            selected[key] = bar
    return list(selected.values())


def actions_as_of(series: Any, as_of: int) -> list[dict]:
    """Latest receipt-version per action; future-effective actions stay explicit."""
    return _actions_as_of_validated(validate_series(series), as_of)


def _actions_as_of_validated(series: dict, as_of: int) -> list[dict]:
    timestamp(as_of, "as_of")
    selected: dict[str, dict] = {}
    ranks: dict[str, tuple[int, int]] = {}
    clocks = receipt_clocks(series, "actions")
    for action in series["actions"]:
        known_at = effective_known_at(series, action, "actions", receipt_index=clocks)
        rank = (known_at, action["observed_at"])
        if known_at <= as_of and (action["id"] not in ranks or rank > ranks[action["id"]]):
            ranks[action["id"]] = rank
            selected[action["id"]] = action
    return sorted(selected.values(), key=lambda a: (a["effective_at"], a["id"]))
