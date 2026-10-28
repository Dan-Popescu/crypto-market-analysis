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
        raise ValueError("Un graphique doit contenir un seul actif ; filtrez les données par actif.")
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


def _complete_daily(candles: list[Candle], as_of: int):
    groups = defaultdict(dict)
    for c in sorted(candles, key=lambda item: item.observed_at):
        if (
            c.close_time <= as_of
            and c.observed_at <= as_of
            and c.open_time >= as_of - 365 * 86400
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
            "Prix indisponible : collectez 24 bougies horaires alignées pour au moins une journée UTC complète."
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
                "Prix hebdomadaire indisponible : collectez sept journées UTC complètes du lundi au dimanche."
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
            label="Clôture journalière complète",
        )
    asset = candles[0].asset
    ax.set_ylabel(f"Prix ({key[1]})")
    vol = ax.twinx()
    vol.bar(dates, volumes, color="#94a3b8", alpha=0.22, width=4 if weekly else 0.7)
    vol.set_ylabel(f"Volume {'hebdomadaire' if weekly else 'journalier'} ({asset})", color="#a3adbd")
    peak, drawdowns = 0.0, []
    for close in closes:
        peak = max(peak, close)
        drawdowns.append(close / peak - 1)
    dd.plot(dates, drawdowns, color="#fb7185", marker="o" if len(dates) == 1 else None)
    dd.yaxis.set_major_formatter(PercentFormatter(1))
    dd.set_ylabel("Repli depuis\nle plus haut")
    ax.set_title(
        f"{'Bougies hebdomadaires' if weekly else 'Prix journalier'} et repli · {asset}/{key[1]} · {key[0]} · UTC",
        loc="left",
    )
    return {key[3]}


def _observed_chart(fig, kind: str, observations: list[Observation]):
    if kind == "liquidity":
        allowed = {"depth_bid_1pct": "Achats", "depth_ask_1pct": "Ventes"}
    elif kind == "leverage":
        allowed = {
            "open_interest_amount": "Intérêt ouvert",
            "open_interest_value": "Intérêt ouvert",
            "funding_rate_actual": "Financement réalisé",
            "funding_rate_projected": "Financement projeté",
        }
    else:
        allowed = {f"option_atm_iv_{d}d": f"IV ATM cible {d} j" for d in (7, 30, 60)}
    rows = [o for o in observations if o.metric in allowed]
    if kind == "options":
        rows = [o for o in rows if o.metadata.get("expiry") is not None]
    if not rows:
        raise ValueError(
            f"Données {kind} indisponibles : collectez les métriques correspondantes avec leurs métadonnées."
        )
    # Separate units and native quote to avoid implicit conversion or normalization.
    panels = defaultdict(list)
    for o in rows:
        if kind == "leverage":
            period = o.metadata.get("funding_interval_hours")
            period_label = f"{period} h" if period is not None else "période inconnue"
            panel = (
                f"Financement · {period_label}" if o.metric.startswith("funding") else "Intérêt ouvert",
                o.unit or "unité inconnue",
                o.quote or "—",
            )
        else:
            panel = (
                "Profondeur à 1 %" if kind == "liquidity" else "Volatilité implicite ATM",
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
                label += f" · {period} h" if period is not None else " · période inconnue"
            if kind == "liquidity" and not o.metadata.get("coverage_complete", False):
                label += " · borne inférieure (couverture incomplète)"
            if kind == "options":
                expiry = o.metadata["expiry"]
                try:
                    label += f" · échéance {_dt(int(expiry)).strftime('%Y-%m-%d')}"
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
            series[label].append((o.event_time, o.value))
        if not series:
            raise ValueError("Options indisponibles : aucune échéance valide dans les métadonnées.")
        for label, points in sorted(series.items()):
            _series(ax, points, label, linestyle="--" if "borne inférieure" in label else "-")
        unit = panel[1]
        if kind == "leverage" and unit == "rate":
            ax.yaxis.set_major_formatter(PercentFormatter(1))
            unit = "% par période de financement"
        elif kind == "options" and unit == "percent":
            unit = "% annualisé"
        ax.set_ylabel(f"{panel[0]}\n({unit})")
        ax.set_title(f"{panel[0]} · cotation {panel[2]}", loc="left", fontsize=11)
        ax.legend(loc="best", fontsize=8)
    fig.suptitle(
        {
            "liquidity": "Liquidité observée",
            "leverage": "Marchés à effet de levier",
            "options": "Options · volatilité implicite cotée",
        }[kind],
        x=0.09,
        ha="left",
        fontsize=15,
    )
    return {o.data_mode for o in rows}


def _calibration(fig, evaluation: dict | None):
    if not evaluation:
        raise ValueError("Calibration indisponible : exécutez une évaluation empirique walk-forward.")
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
        raise ValueError("Calibration indisponible : aucune classe empirique remplie dans l'évaluation.")
    ax = fig.subplots()
    validation_label = (
        "simulation rétrospective"
        if evaluation.get("validation_mode") == "retrospective"
        else "données connues à chaque origine"
    )
    ax.plot([0, 1], [0, 1], color="#94a3b8", linestyle="--", label="Calibration parfaite")
    ax.scatter([p[0] for p in points], [p[1] for p in points], color="#60a5fa", label="Classes observées")
    for x, y, n in points:
        ax.annotate(f"n={n}", (x, y), xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax.set(
        xlim=(0, 1),
        ylim=(0, 1),
        xlabel="Probabilité prévue moyenne",
        ylabel="Fréquence observée",
        title=f"Fiabilité observée · {validation_label}\nClôture en baisse · {evaluation.get('horizon', '?')} j · {evaluation.get('source', 'source non précisée')} / {evaluation.get('quote', 'cotation non précisée')}",
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
) -> Path:
    """Render one PNG or SVG chart, raising ValueError when evidence is missing.

    ``forecast`` is accepted for the shared interface but is never plotted as observed data.
    """
    if kind not in {"price", "weekly", "liquidity", "leverage", "options", "calibration"}:
        raise ValueError(
            "Type inconnu : choisissez price, weekly, liquidity, leverage, options ou calibration."
        )
    output = Path(output)
    if output.suffix.lower() not in {".png", ".svg"}:
        raise ValueError("Le fichier de sortie doit avoir l'extension .png ou .svg.")
    asset = _identity(candles, observations)
    rows = _valid_observations(observations, as_of)
    with plt.style.context("dark_background"), matplotlib.rc_context({"svg.hashsalt": "crypto-market-analysis"}):
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
            mode = "DÉMONSTRATION" if modes & {"demo", "synthetic"} else ", ".join(sorted(modes)).upper()
            fig.get_layout_engine().set(rect=(0, 0.055, 1, 0.945))
            fig.text(
                0.01,
                0.012,
                f"{asset} · UTC · Au {_dt(as_of).strftime('%Y-%m-%d %H:%M')} · Mode : {mode}",
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
