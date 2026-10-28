"""Deterministic daily research and experimental empirical forecasts.

Timestamps in the interchange models are UTC Unix seconds. Daily bars are accepted
only when all 24 distinct, aligned, completed hourly candles are present.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from .evidence import metric_identity
from .models import Candle, Observation

_COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote_volume"]
_EVENTS = ("red", "up", "flat", "down_barrier", "up_barrier")


def _stamp(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def daily_bars(candles: list[Candle]) -> pd.DataFrame:
    """Aggregate valid complete UTC days, retaining gaps instead of filling them."""
    if len({(c.source, c.asset, c.quote, c.data_mode) for c in candles}) > 1:
        raise ValueError("Candles must share the same source, asset, quote currency and mode.")
    days: dict[int, list[Candle]] = {}
    for c in candles:
        day = c.open_time // 86400 * 86400
        days.setdefault(day, []).append(c)
    rows, dates = [], []
    for day, bars in sorted(days.items()):
        bars.sort(key=lambda c: c.open_time)
        if len(bars) != 24 or any(
            c.timeframe != "1h" or c.open_time != day + i * 3600 or c.close_time != c.open_time + 3600
            for i, c in enumerate(bars)
        ):
            continue
        if any(not np.isfinite(v) for c in bars for v in (c.open, c.high, c.low, c.close, c.volume)):
            continue

        def total(name: str, day_bars: list[Candle]) -> float | None:
            values = [getattr(c, name) for c in day_bars]
            return float(sum(values)) if all(v is not None and np.isfinite(v) for v in values) else None

        rows.append(
            [
                bars[0].open,
                max(c.high for c in bars),
                min(c.low for c in bars),
                bars[-1].close,
                sum(c.volume for c in bars),
                total("quote_volume", bars),
                total("taker_buy_quote_volume", bars),
            ]
        )
        dates.append(pd.Timestamp(day, unit="s", tz="UTC"))
    return pd.DataFrame(
        rows, index=pd.DatetimeIndex(dates, tz="UTC", name="date"), columns=_COLUMNS, dtype=float
    )


def _aligned_bars(candles: list[Candle], offset_seconds: int) -> pd.DataFrame:
    """Build contiguous 24-hour bars at a specified UTC hour, preserving actual time."""
    if not offset_seconds:
        return daily_bars(candles)
    shifted = [
        replace(c, open_time=c.open_time - offset_seconds, close_time=c.close_time - offset_seconds)
        for c in candles
    ]
    result = daily_bars(shifted)
    result.index = result.index + pd.Timedelta(seconds=offset_seconds)
    return result


def _asof(candles: list[Candle], as_of: int | None) -> list[Candle]:
    return [c for c in candles if as_of is None or (c.close_time <= as_of and c.observed_at <= as_of)]


def _contiguous(index: pd.DatetimeIndex) -> bool:
    return len(index) < 2 or bool(np.all(np.diff(index.to_numpy()) == np.timedelta64(1, "D")))


def _iso(date: pd.Timestamp) -> str:
    return date.isoformat()


def _safe_metadata(value: object) -> object:
    """Preserve source metadata without exposing non-JSON numbers or objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _safe_metadata(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(v) for v in value]
    return str(value)


def _summarize_observations(
    observations: list[Observation], as_of: int, modes: list[str]
) -> tuple[list[dict], dict]:
    groups: dict[tuple, list[Observation]] = {}
    for obs in observations:
        if (
            obs.event_time > as_of
            or obs.observed_at > as_of
            or (modes and obs.data_mode not in modes)
            or not np.isfinite(obs.value)
        ):
            continue
        expiry = (
            obs.metadata.get("expiry")
            if obs.metric in ("option_open_interest_expiry", "option_downside_minus_upside_iv")
            else None
        )
        key = (*metric_identity(obs), expiry)
        groups.setdefault(key, []).append(obs)
    summaries = []
    source_counts: dict[str, int] = {}
    for group in groups.values():
        group.sort(key=lambda o: (o.event_time, o.observed_at))
        latest = group[-1]
        from .source_catalog import interpret_context
        comparable = interpret_context([latest], as_of)["observations"][0]["comparable"]
        source_counts[latest.source] = source_counts.get(latest.source, 0) + 1
        changes = {}
        for days in (7, 21):
            cutoff = latest.event_time - days * 86400
            prior = next((o for o in reversed(group) if o.event_time <= cutoff), None)
            # Do not silently turn arbitrarily stale readings into a 7/21-day change.
            if not comparable or prior is None or cutoff - prior.event_time > 86400:
                changes[str(days)] = None
            else:
                changes[str(days)] = {
                    "absolute": float(latest.value - prior.value),
                    "relative": float(latest.value / prior.value - 1) if prior.value != 0 else None,
                    "comparison_time": _stamp(prior.event_time),
                }
        summaries.append(
            {
                "source": latest.source,
                "asset": latest.asset,
                "metric": latest.metric,
                "quote": latest.quote,
                "unit": latest.unit,
                "unit_unknown": latest.unit.lower() in ("", "unknown", "inconnu", "unspecified"),
                "comparable": comparable,
                "value": float(latest.value),
                "event_time": _stamp(latest.event_time),
                "observed_at": _stamp(latest.observed_at),
                "data_mode": latest.data_mode,
                "metadata": _safe_metadata(latest.metadata),
                "changes": changes,
                "coverage": {
                    "observations": len(group),
                    "start": _stamp(group[0].event_time),
                    "end": _stamp(latest.event_time),
                },
            }
        )
    summaries.sort(
        key=lambda x: (
            x["asset"] in ("MACRO", "STABLECOINS"),
            x["source"] == "coinmetrics",
            x["event_time"],
            x["source"],
            x["metric"],
        ),
        reverse=True,
    )
    return summaries[:40], {
        "groups_by_source": source_counts,
        "groups_total": len(summaries),
        "groups_returned": min(40, len(summaries)),
    }


def build_analysis(candles: list[Candle], observations: list[Observation], as_of: int) -> dict:
    bars = daily_bars(_asof(candles, as_of))
    modes = sorted({c.data_mode for c in _asof(candles, as_of)})
    result = {
        "as_of": _stamp(as_of),
        "data_mode": modes[0] if len(modes) == 1 else None,
        "evidence": {
            "daily_trend": None,
            "weekly_trend": None,
            "volatility_21d": None,
            "returns": {},
            "spot_pressure": None,
            "observations": [],
            "last_completed_week_candle": None,
        },
        "hypotheses": [],
        "facts": [],
        "contradictions": [],
        "limitations": [
            "Incomplete candles and incomplete days are excluded.",
            "Descriptive indicators do not predict returns.",
        ],
        "coverage": {
            "complete_days": len(bars),
            "start": _iso(bars.index[0]) if len(bars) else None,
            "end": _iso(bars.index[-1] + pd.Timedelta(days=1)) if len(bars) else None,
        },
    }
    if "demo" in modes:
        result["limitations"].append(
            "Synthetic demonstration data: no real-market conclusions."
        )
    e = result["evidence"]
    if len(bars):
        e["latest_close"] = float(bars.close.iloc[-1])
        e["latest_day"] = _iso(bars.index[-1])
        for n in (7, 21, 30, 60):
            window = bars.iloc[-(n + 1) :]
            e["returns"][str(n)] = (
                float(window.close.iloc[-1] / window.close.iloc[0] - 1)
                if len(window) == n + 1 and _contiguous(window.index) and window.close.iloc[0] > 0
                else None
            )
        w = bars.iloc[-22:]
        if len(w) == 22 and _contiguous(w.index) and (w.close > 0).all():
            e["volatility_21d"] = float(np.log(w.close).diff().dropna().std(ddof=1) * np.sqrt(365))
            e["daily_trend"] = (
                "up" if e["returns"]["21"] > 0 else "down" if e["returns"]["21"] < 0 else "flat"
            )
        # Complete weeks only: Monday through Sunday, ending no later than as_of.
        complete = []
        for start, week in bars.groupby(bars.index - pd.to_timedelta(bars.index.day_of_week, unit="D")):
            if (
                len(week) == 7
                and _contiguous(week.index)
                and int((start + pd.Timedelta(days=7)).timestamp()) <= as_of
            ):
                complete.append((start, float(week.close.iloc[-1]), float(week.open.iloc[0])))
        if complete:
            start, close, opening = complete[-1]
            e["last_completed_week_candle"] = {
                "start": _iso(start),
                "end": _iso(start + pd.Timedelta(days=7)),
                "open": opening,
                "close": close,
                "color": "red" if close < opening else "green" if close > opening else "flat",
            }
        if len(complete) >= 2 and complete[-1][0] - complete[-2][0] == pd.Timedelta(days=7):
            change = complete[-1][1] / complete[-2][1] - 1 if complete[-2][1] > 0 else None
            if change is not None:
                e["weekly_trend"] = {
                    "direction": "up" if change > 0 else "down" if change < 0 else "flat",
                    "return": float(change),
                    "week_start": _iso(complete[-1][0]),
                }
        latest = bars.iloc[-1]
        if (
            pd.notna(latest.quote_volume)
            and pd.notna(latest.taker_buy_quote_volume)
            and latest.quote_volume > 0
        ):
            e["spot_pressure"] = {
                "taker_buy_share": float(latest.taker_buy_quote_volume / latest.quote_volume),
                "day": _iso(bars.index[-1]),
                "source": candles[0].source,
            }
        if e["daily_trend"]:
            result["facts"].append(f"21-day trend: {e['daily_trend']}.")
        if e["weekly_trend"] and e["daily_trend"] and e["weekly_trend"]["direction"] != e["daily_trend"]:
            result["contradictions"].append("Daily and weekly directions diverge.")
    if e["spot_pressure"] is None:
        result["limitations"].append("Spot pressure unavailable: taker buy volumes are incomplete or missing.")
    e["observations"], result["coverage"]["observations"] = _summarize_observations(
        observations, as_of, modes
    )
    result["limitations"].append(
        "Observations are descriptive; macro revisions and network data are not used in the backtest."
    )
    return result


def _feature(bars: pd.DataFrame, i: int) -> np.ndarray | None:
    if i < 60:
        return None
    w = bars.iloc[i - 60 : i + 1]
    if not _contiguous(w.index) or (w.close <= 0).any():
        return None
    close = w.close.to_numpy()
    return np.array(
        [close[-1] / close[-22] - 1, np.std(np.diff(np.log(close[-22:])), ddof=1), close[-1] / close[0] - 1]
    )


def _outcome(
    bars: pd.DataFrame, i: int, horizon: int, threshold: float, anchor_price: float | None = None
) -> dict | None:
    w = bars.iloc[i : i + horizon + 1]
    if len(w) != horizon + 1 or not _contiguous(w.index) or w.close.iloc[0] <= 0:
        return None
    start = w.close.iloc[0] if anchor_price is None else anchor_price
    if start <= 0:
        return None
    future = w.iloc[1:]
    return {
        "red": int(future.close.iloc[-1] < start),
        "up": int(future.close.iloc[-1] > start),
        "flat": int(future.close.iloc[-1] == start),
        "down_barrier": int((future.low <= start * (1 - threshold)).any()),
        "up_barrier": int((future.high >= start * (1 + threshold)).any()),
    }


def _wilson(count: int, n: int) -> list[float]:
    z = 1.959963984540054
    p = count / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    width = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [float(max(0, center - width)), float(min(1, center + width))]


def _estimate(
    bars: pd.DataFrame,
    origin: int,
    horizon: int,
    threshold: float,
    cache: list[tuple[np.ndarray | None, dict | None]] | None = None,
) -> dict:
    f = cache[origin][0] if cache is not None else _feature(bars, origin)
    base = {
        "status": "insufficient",
        "origin": _iso(bars.index[origin] + pd.Timedelta(days=1)),
        "training_samples": 0,
        "effective_samples": 0,
        "neighbor_samples": 0,
        "training_start": None,
        "training_end": None,
        "probabilities": None,
        "base_rates": None,
        "reference_frequencies": None,
        "reference_confidence_intervals": None,
        "required_effective_samples": 20,
        "confidence_intervals": None,
        "insufficient_reason": "Requires 20 valid non-overlapping historical windows and 61 days of feature history.",
    }
    if f is None:
        return base
    # Disjoint training outcome windows, and no outcome reaches the forecast origin.
    candidates = []
    for j in range(60, origin - horizon):
        feat, out = (
            cache[j] if cache is not None else (_feature(bars, j), _outcome(bars, j, horizon, threshold))
        )
        if feat is not None and out is not None:
            candidates.append((j, feat, out))
    selected = []
    last = -horizon - 1
    for item in candidates:
        if item[0] - last >= horizon:
            selected.append(item)
            last = item[0]
    base["training_samples"] = len(candidates)
    base["effective_samples"] = len(selected)
    if selected:
        base["training_start"] = _iso(bars.index[selected[0][0]] + pd.Timedelta(days=1))
        base["training_end"] = _iso(bars.index[selected[-1][0] + horizon] + pd.Timedelta(days=1))
        # Descriptive evidence survives the conditional estimator's support gate.
        counts = {key: sum(item[2][key] for item in selected) for key in _EVENTS}
        base["reference_frequencies"] = {
            key: count / len(selected) for key, count in counts.items()
        }
        base["reference_confidence_intervals"] = {
            key: _wilson(count, len(selected)) for key, count in counts.items()
        }
    if len(selected) < 20:
        return base
    x = np.stack([v[1] for v in selected])
    scale = x.std(axis=0)
    scale[scale < 1e-12] = 1
    distance = np.linalg.norm((x - f) / scale, axis=1)
    k = min(50, max(20, int(np.sqrt(len(selected)) * 3)), len(selected))
    nearest = np.argsort(distance, kind="stable")[:k]
    base.update(
        status="experimental",
        neighbor_samples=k,
        insufficient_reason=None,
        confidence_intervals={key: _wilson(sum(selected[n][2][key] for n in nearest), k) for key in _EVENTS},
        probabilities={key: float(np.mean([selected[n][2][key] for n in nearest])) for key in _EVENTS},
        base_rates=base["reference_frequencies"],
    )
    return base


def forecast(
    candles: list[Candle],
    horizons: list[int] | None = None,
    threshold: float = 0.10,
    as_of: int | None = None,
) -> dict:
    """Return empirical evidence and, with sufficient support, conditional estimates.

    Each horizon's reference frequencies and Wilson intervals describe all eligible
    disjoint outcomes, including samples below the 20-outcome conditional gate.
    Sample counts and training dates describe that same selection. Feature warmup,
    gaps and the origin purge apply to both. Legacy base_rates remain available
    only alongside supported conditional probabilities.
    """
    if not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1")
    horizons = [1, 7, 21, 30, 60] if horizons is None else horizons
    if any(not isinstance(h, int) or isinstance(h, bool) or h < 1 for h in horizons):
        raise ValueError("Horizons must be positive integers")
    filtered = _asof(candles, as_of)
    closed_hours = [
        c.close_time
        for c in filtered
        if c.timeframe == "1h" and c.open_time % 3600 == 0 and c.close_time == c.open_time + 3600
    ]
    offset = max(closed_hours) % 86400 if closed_hours else 0
    bars = _aligned_bars(filtered, offset)
    results = {
        str(h): _estimate(bars, len(bars) - 1, h, threshold)
        if len(bars)
        else {
            "status": "insufficient", "probabilities": None, "base_rates": None,
            "reference_frequencies": None, "reference_confidence_intervals": None,
        }
        for h in horizons
    }
    stale = bool(
        as_of is not None
        and len(bars)
        and as_of - int((bars.index[-1] + pd.Timedelta(days=1)).timestamp()) >= 7200
    )
    return {
        "method": "neighboring historical regimes, experimental",
        "bar_alignment": "rolling_24h_at_last_hourly_close",
        "stale": stale,
        "as_of": _stamp(as_of) if as_of is not None else None,
        "data_mode": filtered[0].data_mode if filtered else None,
        "threshold": threshold,
        "anchor_price": float(bars.close.iloc[-1]) if len(bars) else None,
        "anchor_time": _iso(bars.index[-1] + pd.Timedelta(days=1)) if len(bars) else None,
        "coverage": {
            "complete_days": len(bars),
            "start": _iso(bars.index[0]) if len(bars) else None,
            "end": _iso(bars.index[-1] + pd.Timedelta(days=1)) if len(bars) else None,
        },
        "horizons": results,
        "edge": "not evaluated",
        "limitations": [
            "Experimental, uncalibrated estimates; no demonstrated edge.",
            "Red, up and flat compare the endpoint close with the initial close; they are not calendar candle colors.",
            "Horizons start at the displayed last hourly close. Barrier events use the extremes of 24-hour periods; their sequence is unknown.",
            "Macro and network variables are excluded because historical data availability at each origin is unknown.",
            "Windows with missing days are excluded.",
            "Wilson intervals describe neighboring frequencies under an independence assumption; they do not establish calibration.",
            "Available historical prices may include revisions or backfills unknown at the time.",
            *(
                ["The latest complete close is at least two hours old; interfaces apply their configured expiry."]
                if stale
                else []
            ),
            *(
                ["Synthetic demonstration data."]
                if filtered and filtered[0].data_mode == "demo"
                else []
            ),
        ],
    }


def _latest_versions(candles: list[Candle]) -> list[Candle]:
    """Select latest observed version of each source/asset/quote/hour."""
    versions: dict[tuple, Candle] = {}
    for candle in candles:
        key = (
            candle.source,
            candle.asset,
            candle.quote,
            candle.data_mode,
            candle.timeframe,
            candle.open_time,
        )
        previous = versions.get(key)
        if previous is None or candle.observed_at >= previous.observed_at:
            versions[key] = candle
    return list(versions.values())


def _post_issuance_barriers(candles, issued_at, endpoint, anchor_price, threshold):
    """A coarse boundary extreme cannot prove a touch after delayed issuance."""
    rows = [row for row in candles if row.open_time < endpoint and row.close_time > issued_at]
    full = [row for row in rows if row.open_time >= issued_at and row.close_time <= endpoint]
    boundary = [row for row in rows if row not in full]
    result = {}
    for key, crossed in (("down_barrier", lambda row: row.low <= anchor_price * (1 - threshold)),
                         ("up_barrier", lambda row: row.high >= anchor_price * (1 + threshold))):
        result[key] = 1 if any(crossed(row) for row in full) else None if any(crossed(row) for row in boundary) else 0
    return result


def evaluate(
    candles: list[Candle],
    horizon: int = 7,
    threshold: float = 0.10,
    validation: str = "known_at",
    anchor_hour_utc: int = 0,
) -> dict:
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1 or not 0 < threshold < 1:
        raise ValueError("Invalid horizon or threshold")
    if validation not in ("known_at", "retrospective"):
        raise ValueError("validation must be known_at or retrospective")
    if (
        not isinstance(anchor_hour_utc, int)
        or isinstance(anchor_hour_utc, bool)
        or not 0 <= anchor_hour_utc <= 23
    ):
        raise ValueError("anchor_hour_utc must be an integer between 0 and 23")
    offset = anchor_hour_utc * 3600
    bars = _aligned_bars(_latest_versions(candles), offset)
    outcome_hours = _latest_versions(candles)
    records = []
    # Derive the first instant when all 24 hourly bars of each anchor day were known.
    first_seen: dict[int, int] = {}
    for c in candles:
        first_seen[c.open_time] = min(first_seen.get(c.open_time, c.observed_at), c.observed_at)
    issued_by_day: dict[int, int] = {}
    for day in bars.index:
        day_start = int(day.timestamp())
        times = [first_seen.get(day_start + hour * 3600) for hour in range(24)]
        if all(t is not None for t in times):
            issued_by_day[day_start] = max(day_start + 86400, max(times))
    # With a single version per bar and every day timely, every earlier day is
    # known before a later anchor. This makes the cached vintage causal.
    fast = validation == "retrospective" or (
        len(_latest_versions(candles)) == len(candles)
        and all(issued <= day + 86400 + 3600 for day, issued in issued_by_day.items())
    )
    cache = (
        [(_feature(bars, i), _outcome(bars, i, horizon, threshold)) for i in range(len(bars))]
        if fast
        else None
    )
    versions = sorted(candles, key=lambda c: c.observed_at) if not fast else []
    known: dict[tuple, Candle] = {}
    cursor = 0
    last_time = None
    for i in range(60, len(bars) - horizon):
        anchor_time = int((bars.index[i] + pd.Timedelta(days=1)).timestamp())
        issued_at = issued_by_day.get(anchor_time - 86400, anchor_time)
        if validation == "retrospective":
            issued_at = anchor_time
        elif issued_at > anchor_time + 3600:
            continue
        if last_time is not None and anchor_time - last_time < horizon * 86400:
            continue
        if _outcome(bars, i, horizon, threshold) is None:
            continue
        if fast:
            prediction = _estimate(bars, i, horizon, threshold, cache)
            anchor_price = float(bars.close.iloc[i])
        else:
            while cursor < len(versions) and versions[cursor].observed_at <= issued_at:
                c = versions[cursor]
                key = (c.source, c.asset, c.quote, c.data_mode, c.timeframe, c.open_time)
                known[key] = c
                cursor += 1
            snapshot = _aligned_bars([c for c in known.values() if c.close_time <= anchor_time], offset)
            if (
                len(snapshot) < 61
                or int((snapshot.index[-1] + pd.Timedelta(days=1)).timestamp()) != anchor_time
            ):
                continue
            prediction = _estimate(snapshot, len(snapshot) - 1, horizon, threshold)
            anchor_price = float(snapshot.close.iloc[-1])
        if prediction["status"] != "experimental":
            continue
        out = _outcome(bars, i, horizon, threshold, anchor_price)
        if out is None:
            continue
        if issued_at > anchor_time:
            out.update(_post_issuance_barriers(outcome_hours, issued_at, anchor_time + horizon * 86400,
                                              anchor_price, threshold))
        prediction.update(
            anchor_time=_stamp(anchor_time),
            issued_at=_stamp(issued_at),
            anchor_price=anchor_price,
            data_availability_delay_seconds=issued_at - anchor_time,
        )
        records.append((prediction, out))
        last_time = anchor_time
    metrics = {}
    for key in _EVENTS:
        eligible_records = [record for record in records if record[1][key] is not None]
        if not eligible_records:
            metrics[key] = None
            continue
        p = np.array([r[0]["probabilities"][key] for r in eligible_records])
        b = np.array([r[0]["base_rates"][key] for r in eligible_records])
        y = np.array([r[1][key] for r in eligible_records])
        bins = []
        for lo in np.arange(0, 1, 0.2):
            mask = (p >= lo) & (p < lo + 0.2 if lo < 0.8 else p <= 1)
            if mask.any():
                bins.append(
                    {
                        "lower": round(float(lo), 1),
                        "upper": round(float(lo + 0.2), 1),
                        "n": int(mask.sum()),
                        "mean_probability": float(p[mask].mean()),
                        "observed_frequency": float(y[mask].mean()),
                    }
                )
        metrics[key] = {
            "sample_size": len(eligible_records),
            "ambiguous_origins_excluded": len(records) - len(eligible_records),
            "brier": float(np.mean((p - y) ** 2)),
            "base_brier": float(np.mean((b - y) ** 2)),
            "calibration_bins": bins,
        }
    limitations = [
        "Experimental chronological validation; evaluated origins do not overlap.",
        "Sparse samples make probabilities and calibration uncertain.",
        "Outcome scoring uses the latest available future candles; their close may have been revised, but the initial price is the one known at issuance.",
        "Horizons start at the reference hourly close; first-period extremes may include the delay before issuance, so barrier events are ambiguous during that delay.",
        "Red, up and flat compare the endpoint close with the initial close; they do not describe a calendar candle.",
    ]
    if validation == "retrospective":
        limitations.append(
            "Retrospective: feature and training prices may have been backfilled or revised after each origin; historical availability is not verified."
        )
    else:
        limitations.append(
            "Only versions observed by each origin are used for features and training; incomplete archives reduce coverage."
        )
    if candles and candles[0].data_mode == "demo":
        limitations.append("Synthetic demonstration data.")
    return {
        "status": "experimental" if len(records) >= 20 else "insufficient",
        "horizon": horizon,
        "threshold": threshold,
        "validation_mode": validation,
        "anchor_hour_utc": anchor_hour_utc,
        "historical_availability_verified": validation == "known_at",
        "sample_size": len(records),
        "metrics": metrics,
        "origins": [
            {
                "origin": p["issued_at"],
                "issued_at": p["issued_at"],
                "anchor_time": p["anchor_time"],
                "anchor_price": p["anchor_price"],
                "data_availability_delay_seconds": p["data_availability_delay_seconds"],
                "outcomes": outcome,
                "training_start": p["training_start"],
                "training_end": p["training_end"],
                "probabilities": p["probabilities"],
                "base_rates": p["base_rates"],
                "outcome_end": _iso(pd.Timestamp(p["anchor_time"]) + pd.Timedelta(days=horizon)),
            }
            for p, outcome in records
        ],
        "data_mode": candles[0].data_mode if candles else None,
        "source": candles[0].source if candles else None,
        "asset": candles[0].asset if candles else None,
        "quote": candles[0].quote if candles else None,
        "limitations": limitations,
    }
