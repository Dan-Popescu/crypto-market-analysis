"""Reproducible, provenance-labelled charts of observed data.

All timestamps are UTC epoch seconds. No missing series is imputed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle
from matplotlib.ticker import PercentFormatter

from .models import Candle, Observation
from .research import daily_bars


def _dt(t: int) -> datetime:
    return datetime.fromtimestamp(t, UTC)


def _valid_observations(observations: list[Observation], as_of: int) -> list[Observation]:
    return [o for o in observations if o.event_time <= as_of and o.observed_at <= as_of and isfinite(o.value)]


def _identity(candles: list[Candle], observations: list[Observation]) -> str:
    assets = {x.asset for x in [*candles, *observations]}
    if len(assets) > 1:
        raise ValueError("A chart must contain a single asset; filter the data by asset.")
    return next(iter(assets), "")


def _series(ax, points, label: str, *, color=None, linestyle="-"):
    points = sorted(points, key=lambda x: x[0])
    ax.plot(
        [_dt(t) for t, _ in points],
        [v for _, v in points],
        label=label,
        marker="o" if len(points) == 1 else None,
        markersize=4,
        linewidth=1.6,
        color=color,
        linestyle=linestyle,
    )


def _complete_daily(candles: list[Candle], as_of: int, lookback_days: int = 365):
    if (
        isinstance(lookback_days, bool)
        or not isinstance(lookback_days, int)
        or not 1 <= lookback_days <= 3650
    ):
        raise ValueError("lookback_days must be between 1 and 3650.")
    groups = defaultdict(dict)
    for c in sorted(candles, key=lambda item: item.observed_at):
        if (
            c.close_time <= as_of
            and c.observed_at <= as_of
            and c.open_time >= as_of - lookback_days * 86400
            and c.timeframe == "1h"
            and c.close > 0
            and c.volume >= 0
            and all(isfinite(v) for v in (c.open, c.high, c.low, c.close, c.volume))
        ):
            groups[(c.source, c.quote, c.timeframe, c.data_mode)][c.open_time] = c
    candidates = [(key, daily_bars(list(rows.values()))) for key, rows in groups.items()]
    candidates = [(key, bars) for key, bars in candidates if not bars.empty]
    if not candidates:
        raise ValueError(
            "Price unavailable: collect 24 aligned hourly candles for at least one complete UTC day."
        )
    return max(sorted(candidates, key=lambda item: item[0]), key=lambda item: len(item[1]))


def _price(fig, candles: list[Candle], as_of: int, weekly: bool = False):
    key, daily = _complete_daily(candles, as_of)
    if weekly:
        weeks = []
        for start, week in daily.groupby(daily.index - pd.to_timedelta(daily.index.day_of_week, unit="D")):
            if (
                len(week) == 7
                and int((start + pd.Timedelta(days=7)).timestamp()) <= as_of
                and all((week.index[i] - week.index[i - 1]).days == 1 for i in range(1, 7))
            ):
                weeks.append((start, week))
        if not weeks:
            raise ValueError(
                "Weekly price unavailable: collect seven complete UTC days from Monday through Sunday."
            )
        dates = [start.to_pydatetime() for start, _ in weeks]
        closes = [float(w.close.iloc[-1]) for _, w in weeks]
        volumes = [float(w.volume.sum()) for _, w in weeks]
    else:
        dates = [d.to_pydatetime() for d in daily.index]
        closes = daily.close.tolist()
        volumes = daily.volume.tolist()
    ax, dd = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    if weekly:
        for start, week in weeks:
            x = mdates.date2num(start.to_pydatetime())
            op, high, low, close = (
                float(week.open.iloc[0]),
                float(week.high.max()),
                float(week.low.min()),
                float(week.close.iloc[-1]),
            )
            color = "#34d399" if close >= op else "#fb7185"
            ax.vlines(x, low, high, color=color, linewidth=1)
            ax.add_patch(
                Rectangle(
                    (x - 2, min(op, close)),
                    4,
                    max(abs(close - op), high * 1e-6),
                    facecolor=color,
                    edgecolor=color,
                )
            )
        ax.autoscale_view()
    else:
        ax.plot(
            dates,
            closes,
            color="#60a5fa",
            linewidth=1.8,
            marker="o" if len(dates) == 1 else None,
            label="Complete daily close",
        )
    asset = candles[0].asset
    ax.set_ylabel(f"Price ({key[1]})")
    vol = ax.twinx()
    vol.bar(dates, volumes, color="#94a3b8", alpha=0.22, width=4 if weekly else 0.7)
    vol.set_ylabel(f"{'Weekly' if weekly else 'Daily'} volume ({asset})", color="#a3adbd")
    peak, drawdowns = 0.0, []
    for close in closes:
        peak = max(peak, close)
        drawdowns.append(close / peak - 1)
    dd.plot(dates, drawdowns, color="#fb7185", marker="o" if len(dates) == 1 else None)
    dd.yaxis.set_major_formatter(PercentFormatter(1))
    dd.set_ylabel("Drawdown from\npeak")
    ax.set_title(
        f"{'Weekly candles' if weekly else 'Daily price'} and drawdown · {asset}/{key[1]} · {key[0]} · UTC",
        loc="left",
    )
    return {key[3]}


def _observed_chart(fig, kind: str, observations: list[Observation]):
    if kind == "liquidity":
        allowed = {"depth_bid_1pct": "Bids", "depth_ask_1pct": "Asks"}
    elif kind == "leverage":
        allowed = {
            "open_interest_amount": "Open interest",
            "open_interest_value": "Open interest",
            "funding_rate_actual": "Realized funding",
            "funding_rate_projected": "Projected funding",
        }
    else:
        allowed = {f"option_atm_iv_{d}d": f"Target ATM IV {d} d" for d in (7, 30, 60)}
    rows = [o for o in observations if o.metric in allowed]
    if kind == "options":
        rows = [o for o in rows if o.metadata.get("expiry") is not None]
    if not rows:
        raise ValueError(f"{kind} data unavailable: collect the corresponding metrics with their metadata.")
    # Separate units and native quote to avoid implicit conversion or normalization.
    panels = defaultdict(list)
    for o in rows:
        if kind == "leverage":
            period = o.metadata.get("funding_interval_hours")
            period_label = f"{period} h" if period is not None else "unknown period"
            panel = (
                f"Funding · {period_label}" if o.metric.startswith("funding") else "Open interest",
                o.unit or "unknown unit",
                o.quote or "—",
            )
        else:
            panel = (
                "Depth within 1%" if kind == "liquidity" else "ATM implied volatility",
                o.unit,
                o.quote or "—",
            )
        panels[panel].append(o)
    axes = fig.subplots(len(panels), 1, squeeze=False, sharex=True)[:, 0]
    fig.set_size_inches(11, max(4.8, 2.8 * len(panels)))
    for ax, (panel, data) in zip(axes, sorted(panels.items()), strict=True):
        series = defaultdict(list)
        for o in data:
            label = f"{o.source} · {allowed[o.metric]} · {o.data_mode}"
            if kind == "leverage" and o.metric.startswith("funding"):
                period = o.metadata.get("funding_interval_hours")
                label += f" · {period} h" if period is not None else " · unknown period"
            if kind == "liquidity" and not o.metadata.get("coverage_complete", False):
                label += " · lower bound (incomplete coverage)"
            if kind == "options":
                expiry = o.metadata["expiry"]
                try:
                    label += f" · expiry {_dt(int(expiry)).strftime('%Y-%m-%d')}"
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
            series[label].append((o.event_time, o.value))
        if not series:
            raise ValueError("Options unavailable: no valid expiry in the metadata.")
        for label, points in sorted(series.items()):
            _series(ax, points, label, linestyle="--" if "lower bound" in label else "-")
        unit = panel[1]
        if kind == "leverage" and unit == "rate":
            ax.yaxis.set_major_formatter(PercentFormatter(1))
            unit = "% per funding period"
        elif kind == "options" and unit == "percent":
            unit = "% annualized"
        ax.set_ylabel(f"{panel[0]}\n({unit})")
        ax.set_title(f"{panel[0]} · quote {panel[2]}", loc="left", fontsize=11)
        ax.legend(loc="best", fontsize=8)
    fig.suptitle(
        {
            "liquidity": "Observed liquidity",
            "leverage": "Leveraged markets",
            "options": "Options · quoted implied volatility",
        }[kind],
        x=0.09,
        ha="left",
        fontsize=15,
    )
    return {o.data_mode for o in rows}


def _calibration(fig, evaluation: dict | None):
    if not evaluation:
        raise ValueError("Calibration unavailable: run an empirical walk-forward evaluation.")
    metric = (evaluation.get("metrics") or {}).get("red") or {}
    bins = metric.get("calibration_bins", [])
    points = []
    for b in bins:
        try:
            count = int(b.get("n", 0))
            predicted = float(b["mean_probability"])
            observed = float(b["observed_frequency"])
            if count > 0 and 0 <= predicted <= 1 and 0 <= observed <= 1:
                points.append((predicted, observed, count))
        except (KeyError, ValueError, TypeError):
            continue
    if not points:
        raise ValueError("Calibration unavailable: no populated empirical bins in the evaluation.")
    ax = fig.subplots()
    validation_label = (
        "retrospective simulation"
        if evaluation.get("validation_mode") == "retrospective"
        else "data known at each origin"
    )
    ax.plot([0, 1], [0, 1], color="#94a3b8", linestyle="--", label="Perfect calibration")
    ax.scatter([p[0] for p in points], [p[1] for p in points], color="#60a5fa", label="Observed bins")
    for x, y, n in points:
        ax.annotate(f"n={n}", (x, y), xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax.set(
        xlim=(0, 1),
        ylim=(0, 1),
        xlabel="Mean predicted probability",
        ylabel="Observed frequency",
        title=f"Observed reliability · {validation_label}\nEndpoint down · {evaluation.get('horizon', '?')} d · {evaluation.get('source', 'unspecified source')} / {evaluation.get('quote', 'unspecified quote')}",
    )
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.legend()
    return {evaluation.get("data_mode", "live")}


def render_chart(
    kind: str,
    candles: list[Candle],
    observations: list[Observation],
    output: Path,
    as_of: int,
    forecast: dict | None = None,
    evaluation: dict | None = None,
    input_reference: dict | None = None,
) -> Path:
    """Render one PNG or SVG chart, raising ValueError when evidence is missing.

    ``forecast`` is accepted for the shared interface but is never plotted as observed data.
    """
    if kind not in {"price", "weekly", "liquidity", "leverage", "options", "calibration"}:
        raise ValueError("Unknown kind: choose price, weekly, liquidity, leverage, options or calibration.")
    output = Path(output)
    if output.suffix.lower() not in {".png", ".svg"}:
        raise ValueError("The output file must have the extension .png or .svg.")
    asset = _identity(candles, observations)
    rows = _valid_observations(observations, as_of)
    with (
        plt.style.context("dark_background"),
        matplotlib.rc_context({"svg.hashsalt": "crypto-market-analysis"}),
    ):
        fig = plt.figure(figsize=(11, 6), facecolor="#101827", layout="constrained")
        try:
            if kind in {"price", "weekly"}:
                modes = _price(fig, candles, as_of, weekly=kind == "weekly")
            elif kind == "calibration":
                modes = _calibration(fig, evaluation)
            else:
                modes = _observed_chart(fig, kind, rows)
            for ax in fig.axes:
                ax.set_facecolor("#172235")
                ax.grid(alpha=0.17)
                ax.tick_params(labelsize=9)
                if kind != "calibration":
                    lo, hi = ax.dataLim.intervalx
                    if kind not in ("price", "weekly") and isfinite(lo) and isfinite(hi) and hi - lo < 1 / 24:
                        ax.set_xlim(lo - 0.125, hi + 0.125)
                        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M", tz=UTC))
                    else:
                        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d", tz=UTC))
            mode = "DEMONSTRATION" if modes & {"demo", "synthetic"} else ", ".join(sorted(modes)).upper()
            fig.get_layout_engine().set(rect=(0, 0.055, 1, 0.945))
            fig.text(
                0.01,
                0.012,
                f"{asset} · UTC · As of {_dt(as_of).strftime('%Y-%m-%d %H:%M')} · Mode: {mode}"
                + (f" · Input: {input_reference['content_hash'][:16]}" if input_reference else ""),
                fontsize=8,
                color="#cbd5e1",
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(
                output,
                dpi=150,
                facecolor=fig.get_facecolor(),
                metadata={"Date": None}
                if output.suffix.lower() == ".svg"
                else {"Software": "crypto-market-analysis"},
            )
        finally:
            plt.close(fig)
    return output


def _result_time(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return int(parsed.timestamp())
    raise ValueError("Chart timestamps require UTC epochs or timezone-aware ISO dates.")


def _finite_points(rows: list[dict], field: str, as_of: int) -> list[tuple[int, float]]:
    points = []
    for row in rows:
        if row.get(field) is None:
            continue
        time = _result_time(row["time"])
        value = float(row[field])
        if not isfinite(value):
            raise ValueError("Chart values must be finite.")
        if time <= as_of:
            points.append((time, value))
    return points


def _context_result(fig, result: dict, as_of: int, quote: str):
    history = result.get("price_history", [])
    points = _finite_points(history, "close", as_of)
    if not points:
        raise ValueError("Context chart unavailable: no complete observed daily price history.")
    ax, measures = fig.subplots(2, 1, gridspec_kw={"height_ratios": [3, 1]})
    _series(ax, points, "Complete daily close", color="#60a5fa")
    ax.set_ylabel(f"Price ({quote})")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d", tz=UTC))
    ax.set_title("Observed long-history context · complete UTC days", loc="left")
    labels, returns, declines = [], [], []
    for name, row in result.get("lookbacks", {}).items():
        labels.append(str(row.get("lookback_days", name)))
        returns.append((row.get("return") or {}).get("value"))
        declines.append((row.get("decline_from_high") or {}).get("value"))
    for values, label, color, offset in (
        (returns, "Return", "#60a5fa", -0.18),
        (declines, "Decline from observed high", "#fb7185", 0.18),
    ):
        available = [
            (i, float(value))
            for i, value in enumerate(values)
            if value is not None and isfinite(float(value))
        ]
        if available:
            measures.bar(
                [i + offset for i, _ in available],
                [value for _, value in available],
                width=0.35,
                label=label,
                color=color,
            )
    measures.set_xticks(range(len(labels)), labels)
    measures.set_xlabel("Observed lookback (complete UTC days); absent bars mean unavailable")
    measures.set_ylabel("Fraction")
    measures.yaxis.set_major_formatter(PercentFormatter(1))
    if measures.containers:
        measures.legend(fontsize=8)


def _windows_result(fig, result: dict, as_of: int, quote: str):
    anchor = result.get("anchor_price")
    if anchor is None or not isfinite(float(anchor)) or float(anchor) <= 0:
        raise ValueError("Purchase-window chart unavailable: no finite observed anchor.")
    ax, frequency = fig.subplots(2, 1)
    ax.axhline(float(anchor), label="Observed anchor", color="#60a5fa")
    levels = sorted({float(t) for levels in result.get("windows", {}).values() for t in levels})
    for threshold in levels:
        ax.axhline(
            float(anchor) * (1 - threshold), linestyle="--", label=f"Below-anchor threshold {threshold:.1%}"
        )
    ax.set_xticks([])
    ax.set_ylabel(f"Price ({quote})")
    ax.set_title("Purchase thresholds · no future price path", loc="left")
    ax.legend(fontsize=8)
    index = 0
    labels = []
    for horizon, thresholds in result.get("windows", {}).items():
        for threshold, row in thresholds.items():
            conditional = (row.get("conditional_frequencies") or {}).get("below_anchor_touch")
            reference = (row.get("reference_frequencies") or {}).get("below_anchor_touch")
            uncertainty = (row.get("uncertainty") or {}).get("below_anchor_touch")
            if conditional is not None:
                kwargs = {}
                if isinstance(uncertainty, (list, tuple)) and len(uncertainty) == 2:
                    lower, upper = map(float, uncertainty)
                    kwargs["yerr"] = [[max(0, conditional - lower)], [max(0, upper - conditional)]]
                frequency.errorbar(
                    index - 0.08,
                    conditional,
                    fmt="o",
                    color="#60a5fa",
                    label="Conditional" if index == 0 else None,
                    **kwargs,
                )
            if reference is not None:
                frequency.plot(
                    index + 0.08,
                    reference,
                    marker="s",
                    color="#fbbf24",
                    label="Reference" if index == 0 else None,
                )
            labels.append(f"{horizon}d / {float(threshold):.0%}\nn={row.get('effective_samples', 0)}")
            index += 1
    frequency.set_xticks(range(len(labels)), labels, fontsize=7)
    frequency.set_ylim(0, 1)
    frequency.yaxis.set_major_formatter(PercentFormatter(1))
    frequency.set_ylabel("Touch frequency")
    frequency.set_title(
        "Experimental historical frequencies; absent points mean insufficient support", loc="left", fontsize=9
    )
    if frequency.lines:
        frequency.legend(fontsize=8)


def _plans_result(fig, result: dict, as_of: int, quote: str):
    wealth, purchases = fig.subplots(2, 1, sharex=True)
    found = False
    for name, plan in result.get("plans", {}).items():
        trajectory = plan.get("trajectory", [])
        total = _finite_points(trajectory, "total_wealth", as_of)
        reserve = _finite_points(trajectory, "reserve", as_of)
        if total:
            _series(wealth, total, f"{name}: holdings plus reserve")
            found = True
        if reserve:
            _series(wealth, reserve, f"{name}: reserve", linestyle="--")
        history = _finite_points(plan.get("history", []), "price", as_of)
        if history:
            purchases.scatter(
                [_dt(t) for t, _ in history],
                [price for _, price in history],
                label=f"{name}: hypothetical purchases",
                s=20,
            )
    if not found:
        raise ValueError("Plan comparison chart unavailable: no observed valuation trajectory.")
    wealth.set_ylabel(f"Holdings plus reserve ({quote})")
    costs = "; ".join(
        f"{name}: {float(plan['new_purchases_all_in_cost']):,.2f} {quote}"
        if plan.get("new_purchases_all_in_cost") is not None else f"{name}: unknown"
        for name, plan in result.get("plans", {}).items()
    )
    wealth.set_title(
        f"Hypothetical holdings plus reserve · deposits included\nNew purchase all-in cost — {costs}",
        loc="left",
        fontsize=10,
    )
    wealth.legend(fontsize=7)
    purchases.set_ylabel(f"Purchase price ({quote})")
    purchases.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d", tz=UTC))
    if purchases.collections:
        purchases.legend(fontsize=7)
    else:
        purchases.text(
            0.5, 0.5, "No hypothetical purchases available", transform=purchases.transAxes, ha="center"
        )


def render_result_chart(kind: str, result: dict, output: Path) -> Path:
    """Render shared-engine context/windows/plans evidence without recalculating it."""
    if kind not in {"context", "windows", "plans"}:
        raise ValueError("Unknown result chart kind: choose context, windows or plans.")
    output = Path(output)
    if output.suffix.lower() not in {".png", ".svg"}:
        raise ValueError("The output file must have the extension .png or .svg.")
    as_of = _result_time(result.get("as_of"))
    market = result.get("market") or result.get("config") or result
    asset, quote, source = (
        market.get(key) or result.get(key) or "unknown" for key in ("asset", "quote", "source")
    )
    mode = market.get("data_mode", result.get("data_mode", "unknown"))
    mode = "SYNTHETIC DEMONSTRATION" if mode in {"demo", "synthetic"} else mode.upper()
    reference = result.get("input_reference", result.get("input_fingerprint", "unavailable"))
    if isinstance(reference, dict):
        reference = str(reference.get("content_hash", "unavailable"))[:24]
    with (
        plt.style.context("dark_background"),
        matplotlib.rc_context({"svg.hashsalt": "crypto-market-analysis", "svg.fonttype": "none"}),
    ):
        fig = plt.figure(figsize=(12, 8), facecolor="#101827", layout="constrained")
        try:
            {"context": _context_result, "windows": _windows_result, "plans": _plans_result}[kind](
                fig, result, as_of, quote
            )
            for ax in fig.axes:
                ax.set_facecolor("#172235")
                ax.grid(alpha=0.17)
            fig.get_layout_engine().set(rect=(0, 0.07, 1, 0.90))
            fig.suptitle(f"{asset}/{quote} · {source} · {mode}", fontsize=12, y=0.985)
            fig.text(
                0.01,
                0.025,
                f"UTC · As of {_dt(as_of).isoformat()}\nInput reference: {reference}",
                fontsize=7,
                color="#cbd5e1",
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(
                output,
                dpi=150,
                facecolor=fig.get_facecolor(),
                metadata={"Date": None}
                if output.suffix.lower() == ".svg"
                else {"Software": "crypto-market-analysis"},
            )
        finally:
            plt.close(fig)
    return output
