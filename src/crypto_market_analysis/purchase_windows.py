"""Experimental historical purchase windows and issuance-relative observed events.

A historical anchor-relative empirical estimate is separate from the actual
purchase window. Delayed issuance does not silently shorten the latter, and
coarse OHLC extremes never establish exact crossing times or execution fills.
"""

from __future__ import annotations

from datetime import datetime

from .context import (
    _complete_days,
    _eligible_candles,
    _number,
    _positive_ints,
    _stamp,
    _timestamp,
)
from .models import Candle
from .research import _aligned_bars, _feature, _outcome, _wilson, forecast

_DAY = 86400
_EVENT_NAMES = {"red": "endpoint_below_anchor", "down_barrier": "below_anchor_touch"}


def _thresholds(values: list[float]) -> list[float]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 12:
        raise ValueError("thresholds must contain between 1 and 12 values")
    result = [_number(value, "threshold", positive=True) for value in values]
    if any(value >= 1 for value in result) or len(set(result)) != len(result):
        raise ValueError("thresholds must be unique fractions strictly between zero and one")
    return result


def _event(state: str, **fields) -> dict:
    return {
        "state": state,
        "value": True if state == "known_true" else False if state == "known_false" else None,
        **fields,
    }


def resolve_window(
    candles: list[Candle],
    anchor_price: float,
    anchor_time: int,
    issued_at: int,
    horizon_days: int,
    threshold: float,
    as_of: int,
) -> dict:
    """Resolve [issued_at, issued_at + horizon_days*86400] using known closed bars.

    The anchor is an externally frozen completed-hour price. Its timestamp is
    distinct from issuance. An exact endpoint needs a close at the deadline;
    an intrahour deadline remains ambiguous. A threshold extreme in a partial
    boundary bar proves neither a post-issuance touch nor its absence.
    """
    for name, value in (("as_of", as_of), ("anchor_time", anchor_time), ("issued_at", issued_at)):
        _timestamp(value, name)
    anchor_price = _number(anchor_price, "anchor_price", positive=True)
    horizon_days = _positive_ints([horizon_days], "horizon_days", 365, 1)[0]
    threshold = _thresholds([threshold])[0]
    if anchor_time > issued_at:
        raise ValueError("anchor_time cannot follow issued_at")
    end = issued_at + horizon_days * _DAY
    _timestamp(end, "window_end")
    eligible, market = _eligible_candles(candles, as_of)
    overlapping = [c for c in eligible if c.open_time < end and c.close_time > issued_at]
    by_open = {c.open_time: c for c in overlapping}
    first_hour = issued_at // 3600 * 3600
    last_hour = (end + 3599) // 3600 * 3600
    required = (last_hour - first_hour) // 3600
    missing = [t for t in range(first_hour, last_hour, 3600) if t not in by_open]
    mature = as_of >= end
    target = anchor_price * (1 - threshold)
    full = [c for c in overlapping if c.open_time >= issued_at and c.close_time <= end]
    full_opens = {c.open_time for c in full}
    definite = [c for c in full if c.low <= target]
    uncertain = [c for c in overlapping if c.open_time not in full_opens and c.low <= target]
    if definite:
        touch_state = "known_true"
    elif not mature:
        touch_state = "pending"
    elif uncertain or missing:
        touch_state = "ambiguous"
    else:
        touch_state = "known_false"
    crossing = None
    crossing_start = crossing_end = None
    if definite or uncertain:
        first = min(definite + uncertain, key=lambda c: c.open_time)
        crossing_end = min(end, min(c.close_time for c in definite) if definite else first.close_time)
        earlier_missing = any(t < crossing_end for t in missing)
        earlier_uncertain = any(c.open_time < crossing_end for c in uncertain)
        crossing_start = issued_at if earlier_missing else max(issued_at, first.open_time)
        crossing = {
            "start": _stamp(crossing_start),
            "end": _stamp(crossing_end),
            "exact": False,
            "status": "ambiguous" if earlier_uncertain or earlier_missing else "observed_hour_interval",
            "resolution_seconds": 3600,
        }
    endpoint_bar = next((c for c in eligible if c.close_time == end), None)
    if not mature:
        endpoint = _event("pending", price=None, time=_stamp(end), interval=None)
    elif endpoint_bar is not None:
        endpoint = _event(
            "known_true" if endpoint_bar.close < anchor_price else "known_false",
            price=float(endpoint_bar.close),
            time=_stamp(end),
            interval=None,
        )
    else:
        endpoint = _event(
            "ambiguous",
            price=None,
            time=_stamp(end),
            interval={
                "start": _stamp(end // 3600 * 3600),
                "end": _stamp((end + 3599) // 3600 * 3600),
                "reason": "No known completed hourly close exists at the exact window deadline",
            },
        )
    days = _complete_days(eligible)
    calendar_days = []
    for day in range((issued_at + _DAY - 1) // _DAY * _DAY, end - _DAY + 1, _DAY):
        row = days.get(day)
        calendar_days.append(
            {
                "start": _stamp(day),
                "end": _stamp(day + _DAY),
                **(
                    _event(
                        "known_true" if row["close"] < row["open"] else "known_false",
                        open=row["open"],
                        close=row["close"],
                    )
                    if row
                    else _event("pending" if as_of < day + _DAY else "ambiguous")
                ),
            }
        )
    return {
        "schema_version": 1,
        "market": market,
        "as_of": _stamp(as_of),
        "anchor_price": anchor_price,
        "anchor_time": _stamp(anchor_time),
        "issued_at": _stamp(issued_at),
        "horizon_days": horizon_days,
        "window_end": _stamp(end),
        "threshold": threshold,
        "status": "pending"
        if not mature
        else "resolved"
        if endpoint["value"] is not None and touch_state in ("known_true", "known_false")
        else "ambiguous",
        "interval_convention": "[issued_at, window_end]; only fully contained hourly bars prove touches",
        "endpoint_below_anchor": {**endpoint, "definition": "price at window_end < frozen anchor_price"},
        "below_anchor_touch": {
            **_event(touch_state),
            "target_price": target,
            "definition": "observed price <= anchor_price * (1 - threshold) during the issuance-relative window",
            "first_crossing_interval": crossing,
            "time_to_first_crossing_seconds": [crossing_start - issued_at, crossing_end - issued_at]
            if crossing
            else None,
            "barrier_order": "unknown_inside_hourly_ohlc",
        },
        "calendar_down_day": {
            "definition": "complete UTC calendar-day close < that day's open; only days wholly inside the window",
            "timezone": "UTC",
            "days": calendar_days,
            "status": "available"
            if calendar_days and all(d["value"] is not None for d in calendar_days)
            else "unavailable",
        },
        "coverage": {
            "required_overlapping_hours": required,
            "observed_overlapping_hours": len(overlapping),
            "complete_postissuance_hours": len(full),
            "missing_hours": len(missing),
            "boundary_straddling_hours": len(overlapping) - len(full),
            "event_closed_and_observed_by_as_of": True,
        },
        "limitations": [
            "A threshold observation is not proof that a purchase order could have filled.",
            "Extremes inside hourly bars do not reveal exact crossing times or barrier ordering.",
        ],
    }


def _renamed(values: dict | None) -> dict | None:
    return {_EVENT_NAMES[key]: values[key] for key in _EVENT_NAMES} if values is not None else None


def _sparse_reference(candles: list[Candle], horizon: int, threshold: float) -> dict | None:
    """Retain descriptive base rates below the conditional estimator's support gate.

    Use exactly its feature eligibility, purged training range and disjoint
    outcome selection; this does not lower its conditional-frequency sample gate.
    """
    offset = candles[-1].close_time % _DAY if candles else 0
    bars = _aligned_bars(candles, offset)
    selected = []
    last = -horizon - 1
    for index in range(60, len(bars) - 1 - horizon):
        if index - last < horizon or _feature(bars, index) is None:
            continue
        outcome = _outcome(bars, index, horizon, threshold)
        if outcome is not None:
            selected.append(outcome)
            last = index
    return (
        {key: sum(item[key] for item in selected) / len(selected) for key in _EVENT_NAMES}
        if selected
        else None
    )


def analyze_windows(
    candles: list[Candle],
    as_of: int,
    horizons: list[int] | None = None,
    thresholds: list[float] | None = None,
    issued_at: int | None = None,
    max_price_age: int = 7200,
) -> dict:
    """Return bounded experimental frequencies, reference rates and issuance clocks.

    Frequencies retain the existing research estimator's anchor-relative complete
    24-hour outcomes, disjoint training outcomes, 20-effective-sample gate and
    nearest-regime method. Delayed issuance is explicitly a different event clock;
    these estimates do not claim exact issuance-relative calibration.
    """
    _timestamp(as_of, "as_of")
    issued_at = as_of if issued_at is None else _timestamp(issued_at, "issued_at")
    if issued_at < as_of:
        raise ValueError("issued_at cannot precede the evidence as_of cutoff")
    horizons = _positive_ints([1, 3, 7, 14] if horizons is None else horizons, "horizons", 365, 16)
    thresholds = _thresholds([0.02, 0.05, 0.10] if thresholds is None else thresholds)
    max_price_age = _positive_ints([max_price_age], "max_price_age", 7 * _DAY, 1)[0]
    eligible, market = _eligible_candles(candles, as_of)
    anchor = eligible[-1] if eligible else None
    age = issued_at - anchor.close_time if anchor else None
    stale = age is not None and age >= max_price_age
    expires = anchor.close_time + max_price_age if anchor else None
    windows = {str(h): {} for h in horizons}
    coverage = {"eligible_hours": len(eligible), "complete_days": 0, "start": None, "end": None}
    any_support = False
    for threshold in thresholds:
        historical = forecast(eligible, horizons=horizons, threshold=threshold, as_of=as_of)
        coverage.update(historical["coverage"])
        estimate_anchor_time = historical["anchor_time"]
        estimate_anchor_epoch = (
            int(datetime.fromisoformat(estimate_anchor_time).timestamp()) if estimate_anchor_time else None
        )
        current_alignment = bool(anchor and estimate_anchor_time == _stamp(anchor.close_time))
        for horizon in horizons:
            estimate = historical["horizons"][str(horizon)]
            supported = estimate["status"] == "experimental" and current_alignment
            any_support |= supported
            raw_reference = estimate["base_rates"]
            if raw_reference is None and estimate.get("effective_samples", 0):
                raw_reference = _sparse_reference(eligible, horizon, threshold)
            reference = _renamed(raw_reference)
            conditional = _renamed(estimate["probabilities"]) if supported and not stale else None
            effective = estimate.get("effective_samples", 0)
            reference_intervals = (
                {key: _wilson(round(value * effective), effective) for key, value in reference.items()}
                if reference and effective
                else None
            )
            windows[str(horizon)][str(threshold)] = {
                "status": "stale" if stale else "experimental" if supported else "insufficient",
                "horizon_days": horizon,
                "threshold": threshold,
                "conditional_frequencies": conditional,
                "reference_frequencies": reference,
                "uncertainty": _renamed(estimate.get("confidence_intervals")) if conditional else None,
                "reference_uncertainty": reference_intervals,
                "raw_samples": estimate.get("training_samples", 0),
                "effective_samples": effective,
                "neighbor_samples": estimate.get("neighbor_samples", 0),
                "required_effective_samples": 20,
                "training_start": estimate.get("training_start"),
                "training_end": estimate.get("training_end"),
                "historical_alignment": "anchor_relative_complete_24h_bars",
                "historical_event_origin": "completed_hourly_anchor_close",
                "estimate_anchor_time": estimate_anchor_time,
                "estimate_anchor_price": historical["anchor_price"],
                "estimate_window_end": _stamp(estimate_anchor_epoch + horizon * _DAY)
                if estimate_anchor_epoch is not None
                else None,
                "purchase_window_end": _stamp(issued_at + horizon * _DAY),
                "issuance_alignment_matches": bool(current_alignment and issued_at == anchor.close_time),
                "alignment_reason": "Historical estimates start at the anchor; the purchase window starts at actual issuance",
                "uncertainty_method": "Approximate 95% Wilson intervals; regimes are not proven independent or calibrated",
                "insufficient_reason": None
                if supported
                else "Requires 61 contiguous feature days and at least 20 disjoint historical outcomes",
                "frequency_unit": "fraction",
                **market,
            }
    return {
        "schema_version": 1,
        "status": "unavailable"
        if anchor is None
        else "stale"
        if stale
        else "experimental"
        if any_support
        else "insufficient",
        "as_of": _stamp(as_of),
        "issued_at": _stamp(issued_at),
        "market": market,
        "anchor_price": float(anchor.close) if anchor else None,
        "anchor_time": _stamp(anchor.close_time) if anchor else None,
        "anchor_observed_at": _stamp(anchor.observed_at) if anchor else None,
        "anchor_kind": "completed_hourly_close",
        "price_age_seconds": age,
        "max_price_age_seconds": max_price_age,
        "stale": stale,
        "expires_at": _stamp(expires),
        "expiry_reason": "The hourly anchor is invalid at or after the configured maximum age",
        "coverage": coverage,
        "windows": windows,
        "calendar_down_day": {
            "status": "not_estimated",
            "definition": "Complete UTC day closes below its own open",
        },
        "method": "Experimental nearest historical regimes with disjoint outcome windows",
        "limitations": [
            "No predictive edge, calibrated probability or purchase recommendation is established.",
            "Reference frequencies use all eligible disjoint historical outcomes; conditional frequencies use neighboring regimes.",
            "Historical estimates are anchor-relative; delayed issuance is a different event clock and is not calibrated by these frequencies.",
            "Threshold examples are engineering defaults, not optimized purchase levels.",
            "Hourly anchor prices are observations, not current executable purchase quotes.",
            "Historical backfills and revisions may not have been available at their original event times.",
            "Missing feature or outcome days are excluded; sparse support and stale anchors suppress conditional frequencies.",
        ],
    }
