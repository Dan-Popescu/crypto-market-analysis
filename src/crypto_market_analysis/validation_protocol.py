"""Versioned declarations for prospective validation, without journal or market I/O."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .evidence import canonical_json, content_hash
from .plans import decimal_string

PROTOCOL_VERSION = "prospective_validation_v1"
MARKET_FIELDS = ("source", "asset", "quote", "data_mode")
EVENT_FIELDS = ("event", "convention", "time_anchor", "horizon_days", "threshold")
CONVENTIONS = {"endpoint_below_anchor": "exact_deadline_v1",
               "below_anchor_touch": "fully_contained_hourly_bars_v1",
               "observable_endpoint": "hourly_after_deadline_v1"}


def utc_second(value, name="as_of"):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer UTC epoch second")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} must be nonempty text of at most 200 characters")


def fraction(value, name="probability"):
    if type(value) not in (str, int, float) or len(str(value)) > 100:
        raise ValueError(f"Invalid {name}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not result.is_finite() or not 0 <= result <= 1:
        raise ValueError(f"{name} must be a finite fraction from 0 through 1")
    return float(result)


def _declaration(value):
    declaration = deepcopy(value)
    if not isinstance(declaration, dict):
        raise ValueError("Expected a validation declaration")
    _text(declaration.get("name"), "name")
    start = utc_second(declaration.get("starts_at"), "starts_at")
    end = utc_second(declaration.get("ends_at"), "ends_at")
    if start >= end or declaration.get("mode") not in ("prospective", "retrospective"):
        raise ValueError("Declare a positive issuance window and prospective or retrospective mode")
    criterion = declaration.get("criterion", {})
    minimum = criterion.get("minimum_resolved_nonoverlapping")
    if type(minimum) is not int or not 1 <= minimum <= 100:
        raise ValueError("minimum_resolved_nonoverlapping must be from 1 through 100")
    fraction(criterion.get("maximum_missing_fraction"), "maximum_missing_fraction")
    baseline = declaration.get("baseline", {})
    if (baseline.get("family") != "regular" or baseline.get("reserve_yield") != "0"
            or not isinstance(baseline.get("fees"), dict) or not isinstance(baseline.get("execution"), dict)):
        raise ValueError("Declare the regular baseline, zero reserve yield, fees and execution assumptions")
    _text(baseline.get("rule_version"), "baseline rule_version")
    fees, execution = baseline["fees"], baseline["execution"]
    if set(fees) != {"rate", "fixed", "asset"} or set(execution) != {
        "spread_bps", "slippage_bps", "quantity_step", "price_step", "assumption", "timeframe",
        "max_price_age_seconds"
    }:
        raise ValueError("Declare every fee, spread, slippage, rounding and execution assumption explicitly")
    for key in ("spread_bps", "slippage_bps", "quantity_step", "price_step"):
        raw = execution[key]
        if type(raw) not in (str, int) or len(str(raw)) > 100:
            raise ValueError("Invalid execution cost or rounding step")
        try:
            number = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError("Invalid execution cost or rounding step") from exc
        if (not number.is_finite() or number < 0 or abs(number.adjusted()) > 100
                or (key.endswith("step") and number == 0) or (key.endswith("bps") and number > 10000)):
            raise ValueError("Invalid execution cost or rounding step")
        execution[key] = decimal_string(number)
    if execution["assumption"] != "historical_configured_costs":
        raise ValueError("Only historical_configured_costs execution is supported")
    if fraction(fees.get("rate"), "fee rate") >= 1:
        raise ValueError("Fee rate must be below one")
    if type(fees.get("fixed")) not in (str, int) or len(str(fees["fixed"])) > 100:
        raise ValueError("Invalid fixed fee")
    try:
        fixed = Decimal(fees.get("fixed"))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid fixed fee") from exc
    if (not fixed.is_finite() or fixed < 0 or abs(fixed.adjusted()) > 100 or fees.get("asset") not in ("base", "quote")
            or execution.get("timeframe") not in ("1h", "1d")
            or type(execution.get("max_price_age_seconds")) is not int
            or not 0 <= execution["max_price_age_seconds"] <= 86400):
        raise ValueError("Invalid declared baseline costs or execution assumptions")
    fees["rate"], fees["fixed"] = decimal_string(Decimal(str(fees["rate"]))), decimal_string(fixed)
    cohorts = declaration.get("cohorts")
    if not isinstance(cohorts, list) or not 1 <= len(cohorts) <= 1000:
        raise ValueError("Declare from 1 through 1000 separate cohorts")
    identities, markets = set(), set()
    for cohort in cohorts:
        if not isinstance(cohort, dict):
            raise ValueError("Invalid cohort")
        for key in ("cohort_id", *MARKET_FIELDS, "model_version", "rule_version"):
            _text(cohort.get(key), key)
        if cohort["data_mode"] not in ("live", "import", "demo", "synthetic"):
            raise ValueError("Invalid data_mode")
        if ("dataset_lineage" not in cohort or (cohort["dataset_lineage"] is not None
                and (not isinstance(cohort["dataset_lineage"], dict) or not cohort["dataset_lineage"]))):
            raise ValueError("Declare dataset_lineage as a nonempty object or explicit null for the live store")
        if not isinstance(cohort.get("rules"), dict) or not cohort["rules"]:
            raise ValueError("Declare frozen rules")
        market = canonical_json({key: cohort[key] for key in (*MARKET_FIELDS, "dataset_lineage")})
        if cohort["cohort_id"] in identities or market in markets:
            raise ValueError("Cohort IDs and market/lineage identities must be unique")
        identities.add(cohort["cohort_id"])
        markets.add(market)
        events = cohort.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= 32:
            raise ValueError("Declare from 1 through 32 events per cohort")
        seen = set()
        for event in events:
            if not isinstance(event, dict) or set(event) != set(EVENT_FIELDS):
                raise ValueError("Declare every event identity field")
            if (event["event"] not in CONVENTIONS or event["convention"] != CONVENTIONS[event["event"]]
                    or event["time_anchor"] != "issuance" or type(event["horizon_days"]) is not int
                    or not 1 <= event["horizon_days"] <= 365):
                raise ValueError("Unsupported event convention, issuance anchor or horizon")
            if event["event"] == "below_anchor_touch":
                event["threshold"] = fraction(event["threshold"], "threshold")
                if not 0 < event["threshold"] < 1:
                    raise ValueError("Touch threshold must be strictly between zero and one")
            elif event["threshold"] is not None:
                raise ValueError("Endpoint threshold must be null")
            identity = canonical_json(event)
            if identity in seen:
                raise ValueError("Duplicate event declaration")
            seen.add(identity)
    return declaration


def freeze_validation_protocol(declaration: dict, *, frozen_at: int) -> dict:
    """Create a content-addressed declaration; persistence is an explicit caller action.

    The supplied clock is a declaration, not independent capture-time attestation.
    Late freezes are retained honestly and cannot qualify prospective samples.
    """
    core = {"schema_version": 1, "protocol_version": PROTOCOL_VERSION,
            "frozen_at": utc_second(frozen_at, "frozen_at"), "declaration": _declaration(declaration)}
    return {**core, "protocol_hash": content_hash(core)}


def validate_validation_protocol(protocol: dict) -> dict:
    if not isinstance(protocol, dict) or set(protocol) != {
        "schema_version", "protocol_version", "frozen_at", "declaration", "protocol_hash"
    } or protocol.get("schema_version") != 1 or protocol.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unsupported validation protocol")
    expected = freeze_validation_protocol(protocol["declaration"], frozen_at=protocol["frozen_at"])
    if expected["protocol_hash"] != protocol["protocol_hash"]:
        raise ValueError("Validation protocol hash mismatch")
    return expected
