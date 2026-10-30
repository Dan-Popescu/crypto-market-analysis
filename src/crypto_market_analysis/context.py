"""Pure descriptive context for one native-quote hourly reference market.

Trailing measures use complete UTC days. Calendar measures always target the most
recent completed period, even when that period has missing data. No shorter
history, currency conversion, interpolation or stale-period substitution occurs.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from statistics import stdev

from .models import Candle

_DAY = 86400
_DEFAULT_LOOKBACKS = (7, 21, 30, 60, 90, 180, 365, 730)


def _timestamp(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer UTC timestamp")
    try:
        datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"{name} is outside the supported UTC range") from exc
    return value


def _stamp(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def _positive_ints(values: list[int], name: str, maximum: int, count: int) -> list[int]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= count:
        raise ValueError(f"{name} must contain between 1 and {count} values")
    if any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= maximum for v in values):
        raise ValueError(f"{name} must contain integers from 1 to {maximum}")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must contain unique values")
    return list(values)


def _number(value: float, name: str, *, positive: bool = False) -> float:
    try:
        finite = math.isfinite(value) if isinstance(value, (int, float)) else False
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not finite:
        raise ValueError(f"{name} must be a finite number")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


def _eligible_candles(candles: list[Candle], as_of: int) -> tuple[list[Candle], dict]:
    """Select latest known versions, preserving both event and availability cutoffs."""
    selected: dict[int, Candle] = {}
    identities = set()
    for c in candles:
        _timestamp(c.close_time, "candle.close_time")
        _timestamp(c.observed_at, "candle.observed_at")
        if c.close_time > as_of or c.observed_at > as_of:
            continue
        _timestamp(c.open_time, "candle.open_time")
        identities.add((c.source, c.asset, c.quote, c.data_mode))
        if len(identities) > 1:
            raise ValueError("Candles must share source, asset, quote and data mode")
        if c.timeframe != "1h" or c.open_time % 3600 or c.close_time != c.open_time + 3600:
            continue
        for name in ("open", "high", "low", "close"):
            _number(getattr(c, name), f"candle.{name}", positive=True)
        _number(c.volume, "candle.volume")
        if c.low > min(c.open, c.close) or c.high < max(c.open, c.close) or c.low > c.high:
            raise ValueError("Candle prices must lie inside its low/high range")
        old = selected.get(c.open_time)
        if old is None or c.observed_at > old.observed_at:
            selected[c.open_time] = c
        elif c.observed_at == old.observed_at and c != old:
            raise ValueError("Conflicting candle versions have the same availability timestamp")
    market = (
        dict(zip(("source", "asset", "quote", "data_mode"), next(iter(identities)), strict=True))
        if identities
        else {
            "source": None,
            "asset": None,
            "quote": None,
            "data_mode": None,
        }
    )
    return sorted(selected.values(), key=lambda c: c.open_time), market


def _complete_days(candles: list[Candle]) -> dict[int, dict]:
    groups: dict[int, list[Candle]] = {}
    for c in candles:
        groups.setdefault(c.open_time // _DAY * _DAY, []).append(c)
    result = {}
    for start, hours in groups.items():
        if len(hours) != 24 or any(c.open_time != start + i * 3600 for i, c in enumerate(hours)):
            continue
        result[start] = {
            "open": float(hours[0].open),
            "close": float(hours[-1].close),
            "high": float(max(c.high for c in hours)),
            "low": float(min(c.low for c in hours)),
            "volume": float(sum(c.volume for c in hours)),
            "observed_at": max(c.observed_at for c in hours),
        }
    return result


def _coverage(days: dict[int, dict], start: int, end: int, *, closes: bool = False) -> dict:
    required = (end - start) // _DAY
    present = [t for t in range(start, end, _DAY) if t in days]
    return {
        "start": _stamp(start),
        "end": _stamp(end),
        "interval": "[start, end)",
        "required_days": required,
        "obtained_days": len(present),
        "required_closes": required if closes else None,
        "obtained_closes": len(present) if closes else None,
        "missing_days": required - len(present),
        "complete": len(present) == required,
        "first_obtained_close_time": _stamp(present[0] + _DAY) if present else None,
        "last_obtained_close_time": _stamp(present[-1] + _DAY) if present else None,
        "latest_observed_at": _stamp(max(days[t]["observed_at"] for t in present)) if present else None,
    }


def _metric(
    value: float | None, formula: str, unit: str, market: dict, coverage: dict, *, reason: str | None = None
) -> dict:
    complete = coverage["complete"]
    status = "available" if complete else "insufficient" if coverage["obtained_days"] else "unavailable"
    if complete and value is None:
        status = "undefined"
    if value is not None and not math.isfinite(value):
        value, status, reason = None, "undefined", "Arithmetic exceeds finite numeric range"
    return {
        "status": status,
        "value": value if complete else None,
        "formula": formula,
        "unit": unit,
        **market,
        "observation_time": coverage["last_obtained_close_time"],
        "coverage": coverage,
        "reason": reason if reason else (None if complete else "Complete required UTC days are missing"),
    }


def _calendar_bounds(as_of: int, period: str) -> tuple[int, int, int]:
    current = datetime.fromtimestamp(as_of // _DAY * _DAY, UTC)
    if period == "daily":
        end = current
        start = end - timedelta(days=1)
        previous = start - timedelta(days=1)
    elif period == "weekly":
        end = current - timedelta(days=current.weekday())
        start = end - timedelta(days=7)
        previous = start - timedelta(days=7)
    else:
        end = current.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        previous = (start - timedelta(days=1)).replace(day=1)
    return int(previous.timestamp()), int(start.timestamp()), int(end.timestamp())


def _calendar_context(days: dict[int, dict], as_of: int, period: str, market: dict) -> dict:
    previous, start, end = _calendar_bounds(as_of, period)
    coverage = _coverage(days, start, end)
    prior = _coverage(days, previous, start)
    rows = [days[t] for t in range(start, end, _DAY) if t in days]
    volume = sum(row["volume"] for row in rows)
    prior_volume = sum(days[t]["volume"] for t in range(previous, start, _DAY) if t in days)
    ratio_coverage = _coverage(days, previous, end)
    return {
        "start": _stamp(start),
        "end": _stamp(end),
        "timezone": "UTC",
        "coverage": coverage,
        "return": _metric(
            rows[-1]["close"] / rows[0]["open"] - 1 if rows else None,
            "period_close / period_open - 1",
            "fraction",
            market,
            coverage,
        ),
        "volume": _metric(
            volume, "sum(base_volume in complete period)", market["asset"] or "base_asset", market, coverage
        ),
        "previous_period_volume_ratio": _metric(
            volume / prior_volume if prior_volume else None,
            "period_base_volume / previous_complete_calendar_period_base_volume",
            "ratio",
            market,
            ratio_coverage,
            reason="Previous period volume is zero" if prior["complete"] and prior_volume == 0 else None,
        ),
    }


def market_context(candles: list[Candle], as_of: int, lookbacks: list[int] | None = None) -> dict:
    """Describe exact complete-day windows, without investment or valuation labels."""
    _timestamp(as_of, "as_of")
    lookbacks = _positive_ints(
        list(_DEFAULT_LOOKBACKS) if lookbacks is None else lookbacks, "lookbacks", 3650, 16
    )
    eligible, market = _eligible_candles(candles, as_of)
    days = _complete_days(eligible)
    end = as_of // _DAY * _DAY
    windows = {}
    for length in lookbacks:
        start = end - length * _DAY
        close_start = start - _DAY
        coverage = _coverage(days, start, end)
        close_coverage = _coverage(days, close_start, end, closes=True)
        rows = [days[t] for t in range(start, end, _DAY) if t in days]
        closes = [days[t]["close"] for t in range(close_start, end, _DAY) if t in days]
        high = max((row["high"] for row in rows), default=None)
        low = min((row["low"] for row in rows), default=None)
        latest = days.get(end - _DAY)
        price = latest["close"] if latest else None
        log_returns = [math.log(b) - math.log(a) for a, b in zip(closes, closes[1:], strict=False)]
        prior_volumes = [row["volume"] for row in rows[:-1]] if latest else []
        prior_mean = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
        windows[str(length)] = {
            "range_name": f"trailing_{length}_complete_utc_days",
            "lookback_days": length,
            "return": _metric(
                closes[-1] / closes[0] - 1 if closes else None,
                "last_close / first_close - 1",
                "fraction",
                market,
                close_coverage,
            ),
            "decline_from_high": _metric(
                price / high - 1 if price is not None and high else None,
                "last_close / max(hourly_high in lookback) - 1",
                "fraction",
                market,
                coverage,
            ),
            "range_position": _metric(
                (price - low) / (high - low) if price is not None and high != low else None,
                "(last_close - min(hourly_low)) / (max(hourly_high) - min(hourly_low))",
                "fraction",
                market,
                coverage,
                reason="Observed range has zero width" if high == low and rows else None,
            ),
            "realized_volatility": _metric(
                stdev(log_returns) * math.sqrt(365) if len(log_returns) >= 2 else None,
                "sample_std(log(close_t / close_t_minus_1), ddof=1) * sqrt(365)",
                "annualized_fraction",
                market,
                close_coverage,
                reason="At least two daily returns are required" if length < 2 else None,
            ),
            "mean_daily_volume": _metric(
                sum(row["volume"] for row in rows) / length,
                "sum(daily_base_volume) / lookback_days",
                market["asset"] or "base_asset",
                market,
                coverage,
            ),
            "latest_volume_ratio": _metric(
                latest["volume"] / prior_mean if latest and prior_mean else None,
                "last_daily_base_volume / mean(previous lookback_days - 1 daily_base_volumes)",
                "ratio",
                market,
                coverage,
                reason="Previous daily mean volume is zero or absent" if not prior_mean else None,
            ),
        }
    return {
        "schema_version": 1,
        "as_of": _stamp(as_of),
        "status": "available" if days else "unavailable",
        "market": market,
        "anchor": {
            "price": eligible[-1].close,
            "time": _stamp(eligible[-1].close_time),
            "observed_at": _stamp(eligible[-1].observed_at),
            "kind": "completed_hourly_close",
        }
        if eligible
        else None,
        "coverage": {
            "complete_days": len(days),
            "eligible_hours": len(eligible),
            "start": _stamp(min(days)) if days else None,
            "end": _stamp(max(days) + _DAY) if days else None,
            "requested_calendar_end": _stamp(end),
        },
        "lookbacks": windows,
        "calendar": {
            period: _calendar_context(days, as_of, period, market)
            for period in ("daily", "weekly", "monthly")
        },
        "limitations": [
            "Descriptive history does not establish undervaluation or a purchase signal.",
            "Returns require one more complete daily close than the stated day horizon.",
            "Only completed, available hourly candles in one native quote and data mode are used.",
            "Calendar comparisons exclude the current incomplete UTC day, Monday-start week and month.",
            "Historical backfills and revisions do not establish original point-in-time availability.",
        ],
    }
