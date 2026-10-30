"""Opt-in rule-based vigilance preview, without calibrated crash probabilities.

Default baseline: the preceding hour of raw five-minute samples, excluding the
current event, at least nine occupied bins (75% of twelve) for each required
spread/bid-depth/ask-depth series. Medians compare only the same complete native
identity; hourly retention aggregates never substitute for raw snapshots. Price
change uses two completed one-hour closes. These thresholds describe unusual
observations and have no validated forecasting interpretation.

Transitions are pure serializable proposals. Callers explicitly persist their
returned state; no collector, scheduler, messaging service or order path exists.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict

from .evidence import funding_period_seconds, metric_identity
from .models import Candle, Observation

DEFAULT_POLICY = {
    "baseline_seconds": 3600,
    "sample_seconds": 300,
    "minimum_samples": 9,
    "minimum_coverage": 0.75,
    "price_max_age": 3600,
    "liquidity_max_age": 600,
    "derivatives_max_age": 3600,
    "price_drop_pct": 2.0,
    "spread_multiple": 2.0,
    "spread_floor_bps": 5.0,
    "depth_fraction": 0.5,
    "minimum_stress_signals": 2,
    "cooldown_seconds": 3600,
    "recovery_confirmations": 2,
    "watch_seconds": 8 * 3600,
    "poll_seconds": 300,
    "max_records": 100,
}
_REQUIRED = ("spread_bps", "depth_bid_1pct", "depth_ask_1pct")


def _time(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Time must be a nonnegative integer UTC epoch second.")
    return value


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _policy(policy: dict | None) -> dict:
    if policy is not None and (type(policy) is not dict or set(policy) - DEFAULT_POLICY.keys()):
        raise ValueError("Unknown vigilance policy fields.")
    result = {**DEFAULT_POLICY, **(policy or {})}
    for key, low, high in (
        ("baseline_seconds", 300, 7 * 86400), ("sample_seconds", 60, 3600),
        ("minimum_samples", 2, 10000), ("price_max_age", 60, 7200),
        ("liquidity_max_age", 60, 3600), ("derivatives_max_age", 60, 86400),
        ("minimum_stress_signals", 1, 4), ("cooldown_seconds", 0, 86400),
        ("recovery_confirmations", 1, 20), ("watch_seconds", 3600, 24 * 3600),
        ("poll_seconds", 60, 3600), ("max_records", 1, 10000),
    ):
        value = result[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    for key, low, high in (
        ("minimum_coverage", 0.1, 1), ("price_drop_pct", 0.01, 100),
        ("spread_multiple", 1.01, 100), ("spread_floor_bps", 0.01, 10000),
        ("depth_fraction", 0.01, 0.99),
    ):
        value = result[key]
        if not _finite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be a finite number between {low} and {high}.")
    if result["baseline_seconds"] % result["sample_seconds"]:
        raise ValueError("Baseline must contain whole sample intervals.")
    if result["minimum_samples"] > result["baseline_seconds"] // result["sample_seconds"]:
        raise ValueError("Minimum samples exceed baseline sample bins.")
    if result["watch_seconds"] // result["poll_seconds"] + 1 > result["max_records"]:
        raise ValueError("Watch duration and frequency exceed the configured record cap.")
    return result


def _identity(obs: Observation) -> tuple:
    return metric_identity(obs)


def _sample(obs: Observation) -> dict:
    return asdict(obs)


def _known_event(obs: Observation) -> bool:
    # Observation-time fallback is not a verified market event timestamp.
    return obs.metadata.get("timestamp_source") not in ("observation", "unknown")


def _latest_series(observations: list[Observation]) -> list[Observation]:
    versions = {}
    for obs in observations:
        key = (_identity(obs), obs.event_time)
        prior = versions.get(key)
        if prior is None or obs.observed_at > prior.observed_at:
            versions[key] = obs
        elif obs.observed_at == prior.observed_at and obs != prior:
            raise ValueError("Conflicting observations have the same identity and availability time.")
    return sorted(versions.values(), key=lambda row: (row.event_time, row.observed_at))


def _liquidity(
    observations: list[Observation], metric: str, as_of: int, policy: dict
) -> tuple[dict, list[Observation]]:
    groups = {}
    for obs in observations:
        if obs.metric == metric and obs.metadata.get("aggregation") is None:
            groups.setdefault(_identity(obs), []).append(obs)
    if not groups:
        return {"status": "missing", "metric": metric}, []
    # Refuse to pick an arbitrary instrument or mix incompatible book dimensions.
    if len(groups) != 1:
        return {"status": "ambiguous_identity", "metric": metric}, []
    series = _latest_series(next(iter(groups.values())))
    current = series[-1]
    expected_unit = "bps" if metric == "spread_bps" else current.quote
    valid = (
        current.unit == expected_unit and _known_event(current) and current.value >= 0
        and (metric == "spread_bps" or current.metadata.get("coverage_complete") is True)
    )
    expiry = current.event_time + policy["liquidity_max_age"]
    detail = {"metric": metric, "current": _sample(current), "expires_at": expiry}
    if not valid or as_of >= expiry:
        return {**detail, "status": "invalid" if not valid else "stale"}, [current]
    start = current.event_time - policy["baseline_seconds"]
    baseline = [obs for obs in series if start <= obs.event_time < current.event_time and (
        _known_event(obs) and obs.value > 0
        and (metric == "spread_bps" or obs.metadata.get("coverage_complete") is True)
    )]
    # One latest event per fixed bin prevents burst sampling from faking coverage.
    bins = {}
    for obs in baseline:
        bins[(obs.event_time - start) // policy["sample_seconds"]] = obs
    baseline = list(bins.values())
    expected = policy["baseline_seconds"] // policy["sample_seconds"]
    coverage = len(baseline) / expected
    detail.update({"baseline_start": start, "baseline_end": current.event_time,
                   "baseline_samples": len(baseline), "baseline_expected_bins": expected,
                   "baseline_coverage": coverage, "baseline_method": "median_of_latest_raw_event_per_bin"})
    if len(baseline) < policy["minimum_samples"] or coverage < policy["minimum_coverage"]:
        return {**detail, "status": "insufficient_baseline"}, [*baseline, current]
    median = statistics.median(obs.value for obs in baseline)
    ratio = current.value / median
    detail.update({"status": "ok", "baseline_median": median, "ratio_to_baseline": ratio})
    return detail, [*baseline, current]


def _derivatives(observations: list[Observation], as_of: int, policy: dict) -> tuple[list[dict], list[Observation]]:
    groups = {}
    for obs in observations:
        if obs.metric.startswith(("funding_rate_", "open_interest_")):
            groups.setdefault(_identity(obs), []).append(obs)
    results, consumed = [], []
    for identity in sorted(groups, key=str):
        series = _latest_series(groups[identity])
        current = series[-1]
        native = current.unit not in ("unknown", "", None) and bool(current.metadata.get("symbol"))
        if current.metric.startswith("funding_rate_"):
            period = funding_period_seconds(current.metadata)
            native = native and current.unit == "rate" and period is not None
        valid = native and _known_event(current)
        expiry = current.event_time + policy["derivatives_max_age"]
        result = {"current": _sample(current), "status": "ok" if valid and as_of < expiry else
                  "unknown_native_convention" if not valid else "stale", "expires_at": expiry,
                  "interpretation": "Source-specific native units; no inferred leverage or liquidation map."}
        consumed.append(current)
        if valid and len(series) > 1:
            prior = series[-2]
            result["prior"] = _sample(prior)
            result["native_change"] = current.value - prior.value
            consumed.append(prior)
        results.append(result)
    return results, consumed


def assess_vigilance(
    candles: list[Candle], observations: list[Observation], as_of: int,
    horizon_hours: int = 4, policy: dict | None = None,
) -> dict:
    """Assess one selected native market; return finite evidence and expiry."""
    cutoff, rules = _time(as_of), _policy(policy)
    if type(horizon_hours) is not int or horizon_hours not in (4, 8):
        raise ValueError("Vigilance horizon must be 4 or 8 hours.")
    if len(candles) > 20000 or len(observations) > 100000:
        raise ValueError("Vigilance inputs exceed the bounded history limit.")
    for candle in candles:
        _time(candle.open_time)
        _time(candle.close_time)
        _time(candle.observed_at)
        if not all(_finite(value) for value in (candle.open, candle.high, candle.low, candle.close)):
            raise ValueError("Candle prices must be finite numbers.")
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            raise ValueError("Candle prices must be positive.")
        if (candle.timeframe != "1h" or candle.close_time - candle.open_time != 3600
                or candle.open_time % 3600 or candle.observed_at < candle.close_time):
            raise ValueError("Vigilance requires completed aligned one-hour UTC candles.")
        if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            raise ValueError("Inconsistent candle price range.")
    for obs in observations:
        _time(obs.event_time)
        _time(obs.observed_at)
        if not _finite(obs.value) or type(obs.metadata) is not dict:
            raise ValueError("Observations require finite native values and JSON metadata.")
    available = [c for c in candles if c.close_time <= cutoff and c.observed_at <= cutoff]
    identities = {(c.source, c.asset, c.quote, c.data_mode) for c in available}
    if len(identities) > 1:
        raise ValueError("Select one source, asset, quote and data mode before vigilance assessment.")
    market = dict(zip(("source", "asset", "quote", "data_mode"), next(iter(identities)), strict=True)) if identities else None
    price_rows = {}
    for candle in available:
        prior = price_rows.get(candle.close_time)
        if prior is None or candle.observed_at > prior.observed_at:
            price_rows[candle.close_time] = candle
        elif candle.observed_at == prior.observed_at and candle != prior:
            raise ValueError("Conflicting price revisions have identical availability time.")
    prices = sorted(price_rows.values(), key=lambda row: row.close_time)
    price, consumed_candles = {"status": "missing"}, []
    if prices:
        current = prices[-1]
        consumed_candles = [current]
        expiry = current.close_time + rules["price_max_age"]
        price = {"status": "stale" if cutoff >= expiry else "insufficient_history",
                 "current": asdict(current), "expires_at": expiry}
        prior = price_rows.get(current.close_time - 3600)
        if prior is not None:
            consumed_candles.insert(0, prior)
            price.update({"prior": asdict(prior), "change_pct": (current.close / prior.close - 1) * 100})
            if cutoff < expiry:
                price["status"] = "ok"
    relevant = [obs for obs in observations if market and obs.asset == market["asset"]
                and obs.quote == market["quote"] and obs.data_mode == market["data_mode"]
                and obs.event_time <= cutoff and obs.observed_at <= cutoff and obs.event_time <= obs.observed_at]
    liquidity, consumed = {}, []
    for metric in _REQUIRED:
        detail, used = _liquidity([obs for obs in relevant if obs.source == market["source"]], metric, cutoff, rules)
        liquidity[metric] = detail
        consumed.extend(used)
    derivatives, derivatives_used = _derivatives(relevant, cutoff, rules)
    consumed.extend(derivatives_used)
    missing = [{"input": "price", "status": price["status"]}] if price["status"] != "ok" else []
    missing.extend({"input": metric, "status": detail["status"]} for metric, detail in liquidity.items()
                   if detail["status"] != "ok")
    for family in ("funding_rate_", "open_interest_"):
        family_details = [item for item in derivatives if item["current"]["metric"].startswith(family)]
        if not family_details or all(item["status"] != "ok" for item in family_details):
            missing.append({"input": family.rstrip("_"), "status": "optional_unavailable"})
    missing.append({"input": "liquidations", "status": "unavailable_no_verified_feed"})
    required_bad = price["status"] != "ok" or any(d["status"] != "ok" for d in liquidity.values())
    quality = "insufficient" if price["status"] != "ok" else "degraded" if required_bad else "sufficient"
    reasons, contradictions = [], []
    if price["status"] == "ok":
        (reasons if price["change_pct"] <= -rules["price_drop_pct"] else contradictions).append({
            "signal": "rapid_price_decline", "value_pct": price["change_pct"], "threshold_pct": -rules["price_drop_pct"]})
    for metric, detail in liquidity.items():
        if detail["status"] != "ok":
            continue
        ratio = detail["ratio_to_baseline"]
        adverse = (ratio >= rules["spread_multiple"] and detail["current"]["value"] >= rules["spread_floor_bps"]
                   if metric == "spread_bps" else ratio <= rules["depth_fraction"])
        (reasons if adverse else contradictions).append({"signal": metric, "ratio_to_baseline": ratio,
                                                        "current_value": detail["current"]["value"]})
    elevated = (price["status"] == "ok" and len(reasons) >= rules["minimum_stress_signals"]
                and any(reason["signal"] in _REQUIRED for reason in reasons))
    expiries = [cutoff + horizon_hours * 3600]
    if "expires_at" in price:
        expiries.append(price["expires_at"])
    expiries.extend(d["expires_at"] for d in liquidity.values() if "expires_at" in d)
    expires = min(expiries) if not required_bad else cutoff
    # Dedup ignores polling and ingestion clocks, preserving actual event revisions.
    frozen = {"candles": [asdict(c) for c in consumed_candles], "observations": [_sample(o) for o in consumed]}
    fingerprint_evidence = {key: [{k: v for k, v in item.items() if k != "observed_at"} for item in values]
                            for key, values in frozen.items()}
    result = {
        "schema_version": 1, "rules_version": "observational-vigilance-v1", "as_of": cutoff,
        "issued_at": None, "horizon_hours": horizon_hours, "expires_at": expires,
        "state": "elevated" if elevated else "normal", "quality": quality,
        "market": market, "policy": rules, "price": price, "liquidity": liquidity,
        "derivatives": derivatives, "reasons": reasons, "supporting_observations": reasons,
        "contradictions": contradictions, "missing": missing, "evidence": frozen,
        "evidence_fingerprint": _hash({"market": market, "rules": rules, "horizon": horizon_hours,
                                        "evidence": fingerprint_evidence}),
        "interpretation": "Observed fragility only; normal does not establish low risk. No calibrated crash probability.",
    }
    # Reject malformed external metadata or numerical overflow at the public result boundary.
    try:
        encoded = json.dumps(result, allow_nan=False, sort_keys=True)
        if len(encoded.encode()) > 32 * 1024 * 1024:
            raise ValueError("Vigilance result exceeds the 32 MiB limit.")
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Vigilance evidence must contain finite bounded JSON metadata.") from exc
    return result


def transition_vigilance(previous: dict | None, assessment: dict, now: int, policy: dict | None = None) -> dict:
    """Propose a restart-safe state and optional alert, without any side effects.

    Identical evidence cannot emit again or extend expiry. Recovery requires fresh,
    sufficient normal evidence on distinct fingerprints. A watch has a fixed end
    and record cap; explicit new local watch configuration is needed after expiry.
    """
    clock = _time(now)
    rules = _policy(policy if policy is not None else assessment.get("policy"))
    if assessment.get("state") not in ("normal", "elevated"):
        raise ValueError("Assessment state must be normal or elevated.")
    if assessment.get("quality") not in ("sufficient", "degraded", "insufficient"):
        raise ValueError("Invalid assessment quality.")
    fingerprint = assessment.get("evidence_fingerprint")
    if type(fingerprint) is not str or len(fingerprint) != 64:
        raise ValueError("Assessment requires its evidence fingerprint.")
    cutoff, expiry = _time(assessment["as_of"]), _time(assessment["expires_at"])
    if cutoff > clock:
        raise ValueError("Cannot transition from a future assessment.")
    prior = json.loads(json.dumps(previous, allow_nan=False)) if previous is not None else None
    if prior is not None and clock < _time(prior["last_checked_at"]):
        raise ValueError("Transition time cannot move backwards.")
    if prior is not None and (prior["market"] != assessment.get("market")
                              or prior["horizon_hours"] != assessment.get("horizon_hours")):
        raise ValueError("A persisted watch cannot change market identity or horizon.")
    if prior is not None and prior["policy"] != rules:
        raise ValueError("A persisted watch cannot change policy; start a new explicit watch.")
    state = prior or {
        "schema_version": 1, "state": "normal", "quality": assessment["quality"], "policy": rules,
        "market": assessment.get("market"), "horizon_hours": assessment.get("horizon_hours"),
        "watch_started_at": clock, "watch_expires_at": clock + rules["watch_seconds"],
        "assessment_expires_at": expiry, "evidence_fingerprint": None,
        "seen_fingerprints": [], "records_used": 0, "recovery_count": 0,
        "last_alert_at": None, "cooldown_until": 0, "last_checked_at": clock, "last_evaluated_at": clock,
    }
    last_evaluated_at = state["last_evaluated_at"]
    if clock >= state["assessment_expires_at"]:
        state["state"] = "expired"
    state["last_checked_at"] = clock
    state["quality"] = assessment["quality"]
    event = None
    if clock >= state["watch_expires_at"] or state["records_used"] >= rules["max_records"]:
        state["state"] = "expired"
        reason = "watch_expired" if clock >= state["watch_expires_at"] else "record_cap_reached"
    elif fingerprint in state["seen_fingerprints"]:
        if clock >= state["assessment_expires_at"]:
            state["state"] = "expired"
        reason = "duplicate_evidence"
    elif prior is not None and clock - last_evaluated_at < rules["poll_seconds"]:
        reason = "poll_interval_not_elapsed"
    else:
        state["records_used"] += 1
        state["last_evaluated_at"] = clock
        state["seen_fingerprints"].append(fingerprint)
        state["evidence_fingerprint"] = fingerprint
        state["assessment_expires_at"] = min(expiry, state["watch_expires_at"])
        actionable = assessment["quality"] == "sufficient" and clock < state["assessment_expires_at"]
        if not actionable:
            state["state"] = "expired"
            state["recovery_count"] = 0
            reason = "insufficient_or_expired_evidence"
        elif assessment["state"] == "elevated":
            was_elevated = state["state"] == "elevated"
            state["state"] = "elevated"
            state["recovery_count"] = 0
            if not was_elevated and clock >= state["cooldown_until"]:
                event = {"kind": "elevated_vigilance", "issued_at": clock,
                         "expires_at": state["assessment_expires_at"], "evidence_fingerprint": fingerprint,
                         "reasons": assessment.get("reasons", []), "market": assessment.get("market")}
                state["last_alert_at"] = clock
                state["cooldown_until"] = clock + rules["cooldown_seconds"]
                reason = "elevated_transition"
            else:
                reason = "elevated_continues" if was_elevated else "cooldown"
        elif state["state"] == "elevated":
            state["recovery_count"] += 1
            if state["recovery_count"] >= rules["recovery_confirmations"]:
                state["state"] = "normal"
                reason = "recovered"
            else:
                reason = "recovery_pending"
        else:
            state["state"] = "normal"
            state["recovery_count"] = 0
            reason = "normal_observed"
    state["transition_reason"] = reason
    state["alert"] = event
    return state
