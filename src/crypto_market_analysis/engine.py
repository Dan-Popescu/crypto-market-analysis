"""One read-only analytical interface shared by CLI and MCP."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .models import Candle, Observation
from .research import build_analysis, daily_bars, evaluate, forecast
from .storage import Store


def stamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class Engine:
    def __init__(self, settings: Settings, mode: str = "live"):
        if mode not in ("live", "demo", "import"):
            raise ValueError("Invalid mode.")
        self.settings = settings
        self.store = Store(settings.database)
        self.mode = mode

    def selected_candles(
        self,
        asset: str,
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        watermark: dict | None = None,
    ) -> tuple[list[Candle], list[dict]]:
        if asset not in ("BTC", "ETH"):
            raise ValueError("Choose BTC or ETH.")
        candles = self.store.candles(
            asset, source, quote, as_of, self.mode, max_id=watermark["candles"] if watermark else None
        )
        grouped = defaultdict(list)
        for candle in candles:
            grouped[(candle.source, candle.quote)].append(candle)
        coverage = []
        for (venue, currency), bars in grouped.items():
            coverage.append(
                {
                    "source": venue,
                    "quote": currency,
                    "hourly_candles": len(bars),
                    "complete_days": len(daily_bars(bars)),
                    "last_close_time": max(c.close_time for c in bars),
                }
            )
        # A stale long history must not hide a source with recent usable data.
        cutoff = int(time.time()) if as_of is None else as_of
        coverage.sort(
            key=lambda row: (
                row["last_close_time"] >= cutoff - 172800,
                row["complete_days"],
                row["last_close_time"],
                row["source"],
            ),
            reverse=True,
        )
        if not coverage:
            return [], []
        selected = coverage[0]
        return grouped[(selected["source"], selected["quote"])], coverage

    def status(self, as_of: int | None = None) -> dict:
        now = int(time.time()) if as_of is None else as_of
        runs = self.store.runs(now)
        for run in runs:
            run["age_seconds"] = now - run["observed_at"]
            periods = [
                self.settings.liquidity_interval
                if t == "liquidity"
                else self.settings.market_interval
                if t == "market"
                else self.settings.options_interval
                if t == "options"
                else self.settings.context_interval
                for t in run["task"].split("+")
            ]
            run["stale"] = run["age_seconds"] > max(periods) * 2
        observed = {r["source"] for r in runs}
        return {
            "as_of": stamp(now),
            "data_mode": self.mode,
            "counts": self.store.counts(),
            "runs": runs,
            "configured_venues": self.settings.venues,
            "never_collected": [v for v in self.settings.venues if v not in observed],
            "paid_collection_apis": False,
        }

    def inputs(
        self, asset: str, source: str | None = None, quote: str | None = None, as_of: int | None = None
    ) -> tuple[list[Candle], list[Observation], list[dict], dict]:
        now = int(time.time()) if as_of is None else as_of
        watermark = self.store.watermark()
        candles, coverage = self.selected_candles(asset, source, quote, now, watermark)
        observations = self.store.observations(asset, now, self.mode, max_id=watermark["observations"])
        return candles, observations, coverage, watermark

    def bundle(
        self,
        asset: str = "BTC",
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        include_forecast: bool = True,
    ) -> dict:
        now = int(time.time()) if as_of is None else as_of
        candles, observations, coverage, watermark = self.inputs(asset, source, quote, now)
        context = [
            o
            for o in self.store.observations(
                as_of=now, mode=self.mode, max_id=watermark["observations"], days=365
            )
            if o.asset in ("MACRO", "STABLECOINS")
        ]
        analysis = build_analysis(candles, [*observations, *context], now)
        latest = {}
        for o in sorted(
            [*observations, *context], key=lambda row: (row.event_time, row.observed_at), reverse=True
        ):
            dimension = (o.source, o.asset, o.metric, o.quote, o.metadata.get("expiry"))
            if dimension in latest:
                continue
            age = now - o.event_time
            expected = (
                self.settings.options_interval
                if o.source == "deribit"
                else self.settings.context_interval
                if o.asset in ("MACRO", "STABLECOINS") or o.source == "coinmetrics"
                else self.settings.market_interval
            )
            latest[dimension] = {
                **asdict(o),
                "age_seconds": age,
                "stale": age > max(expected * 3, 86400 if o.asset == "MACRO" else 0),
            }
        fingerprint = json.dumps(
            {
                "as_of": now,
                "watermark": watermark,
                "asset": asset,
                "source": candles[0].source if candles else None,
                "mode": self.mode,
            },
            sort_keys=True,
        )
        return {
            "schema_version": "1.0",
            "snapshot_id": hashlib.sha256(fingerprint.encode()).hexdigest()[:16],
            "as_of": stamp(now),
            "as_of_epoch": now,
            "data_mode": self.mode,
            "asset": asset,
            "selected_market": {"source": candles[0].source, "quote": candles[0].quote} if candles else None,
            "market_coverage": coverage,
            "analysis": analysis,
            "forecast": forecast(candles, as_of=now) if include_forecast else None,
            "latest_metrics": list(latest.values())[:150],
            "events": self.store.events(now),
            "source_health": self.status(now),
            "limitations": [
                "No implicit USD/USDT/USDC/EUR conversion or price averaging across venues.",
                "Forecasts use 24-hour windows anchored to the last known hourly close, which may differ from the current price.",
                "Calendar and ETF flows: sourced manual imports; no claim of a complete automated feed.",
                "Missing data is not a neutral signal.",
            ],
        }

    def predictions(
        self,
        asset: str,
        horizons: list[int],
        threshold: float,
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
    ) -> dict:
        now = int(time.time()) if as_of is None else as_of
        rows, coverage = self.selected_candles(asset, source, quote, now)
        result = forecast(rows, horizons, threshold, now)
        return {
            **result,
            "asset": asset,
            "selected_market": {"source": rows[0].source, "quote": rows[0].quote} if rows else None,
            "market_coverage": coverage,
        }

    def evaluation(
        self,
        asset: str,
        horizon: int,
        threshold: float,
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        validation: str = "known_at",
    ) -> dict:
        rows, _ = self.selected_candles(asset, source, quote, as_of)
        alignment = max((c.close_time for c in rows), default=0) % 86400 // 3600
        if rows:
            rows = self.store.candles(asset, rows[0].source, rows[0].quote, as_of, self.mode, versions=True)
        return evaluate(rows, horizon, threshold, validation, anchor_hour_utc=alignment)

    def chart(
        self,
        kind: str,
        asset: str,
        output: Path,
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        horizon: int = 7,
        validation: str = "known_at",
    ) -> Path:
        from .charts import render_chart

        now = int(time.time()) if as_of is None else as_of
        candles, observations, _, _ = self.inputs(asset, source, quote, now)
        if source:
            observations = [o for o in observations if o.source == source]
        if quote:
            observations = [o for o in observations if o.quote == quote]
        evaluation = (
            self.evaluation(asset, horizon, 0.10, source, quote, now, validation)
            if kind == "calibration"
            else None
        )
        return render_chart(kind, candles, observations, output, now, evaluation=evaluation)


def render_brief(bundle: dict, cadence: str = "daily") -> str:
    """Concise deterministic English report; the host LLM can expand from evidence."""
    analysis = bundle["analysis"]
    evidence = analysis["evidence"]
    market = bundle["selected_market"]
    lines = [
        f"# {bundle['asset']} — {'weekly' if cadence == 'weekly' else 'daily'} report",
        f"As of {bundle['as_of']} · mode **{bundle['data_mode']}** · reference {bundle['snapshot_id']}",
        "",
    ]
    if bundle["data_mode"] == "demo":
        lines += ["**SYNTHETIC DEMONSTRATION — no real-market analysis.**", ""]
    if not market:
        lines += ["Insufficient price data: collect or import completed hourly candles."]
    else:
        lines += [
            f"Reference market: {market['source']} · {bundle['asset']}/{market['quote']}.",
            f"History: {analysis['coverage']['complete_days']} complete UTC days.",
        ]
        if evidence.get("latest_close") is not None:
            lines += [
                f"Latest daily close: {evidence['latest_close']:,.2f} {market['quote']} on {evidence['latest_day'][:10]}, UTC."
            ]
        lines += ["", "## Observed evidence", ""]
        for day, value in evidence["returns"].items():
            if value is not None:
                lines.append(f"- {day}-day return: {value:+.1%}.")
        if evidence["volatility_21d"] is not None:
            lines.append(
                f"- Annualized 21-day realized volatility: {evidence['volatility_21d']:.1%}; it describes magnitude, not direction."
            )
        for fact in analysis["facts"]:
            lines.append(f"- {fact}")
    if analysis["contradictions"]:
        lines += ["", "## Contradictory evidence", "", *[f"- {x}" for x in analysis["contradictions"]]]
    labels = {
        "DFII10": "US ten-year real yield",
        "DTWEXBGS": "Broad dollar index",
        "stablecoin_supply": "Dollar-pegged stablecoin supply",
        "funding_rate_projected": "Projected perpetual funding",
        "open_interest_value": "Open interest",
        "option_atm_iv_30d": "Implied volatility near 30 days",
    }
    shown = set()
    context_lines = []
    for point in evidence["observations"]:
        metric = point["metric"]
        if metric not in labels or metric in shown:
            continue
        shown.add(metric)
        value, unit = point["value"], point["unit"]
        if unit == "rate":
            formatted = f"{value:.3%}"
            period = point["metadata"].get("funding_interval_hours")
            formatted += f" over {period:g} hours" if period else " (undocumented period)"
        elif unit == "percent":
            formatted = f"{value:.2f} %"
        elif unit == "index":
            formatted = f"{value:.2f} index points"
        elif unit == "unknown":
            formatted = "undocumented unit; value cannot be interpreted"
        else:
            formatted = f"{value:,.2f} {unit}"
        change = point.get("changes", {}).get("7")
        delta = (
            f"; 7-day change: {change['relative']:+.1%}"
            if change and change.get("relative") is not None and metric == "stablecoin_supply"
            else ""
        )
        context_lines.append(
            f"- {labels[metric]}: {formatted}{delta} — {point['source']}, observed on {point['event_time'][:10]}."
        )
    if context_lines:
        lines += ["", "## Observed context", "", *context_lines]
    prediction = bundle.get("forecast")
    if prediction:
        lines += [
            "",
            "## Experimental scenarios",
            "",
            "Predictive edge not demonstrated; 24-hour windows aligned with the last known hourly close.",
            "",
        ]
        if prediction.get("anchor_price") is not None:
            currency = market["quote"] if market else ""
            lines.append(
                f"Scenario anchor: {prediction['anchor_price']:,.2f} {currency}, as of {prediction['anchor_time']}."
            )
        if prediction.get("stale"):
            lines.append("Warning: the latest available close is stale; refresh collection.")
        lines.append("")
        for horizon, row in prediction["horizons"].items():
            probabilities = row.get("probabilities")
            if probabilities:
                lines.append(
                    f"- {horizon} d: endpoint down {probabilities['red']:.0%}, up {probabilities['up']:.0%}; touch −{prediction['threshold']:.0%} {probabilities['down_barrier']:.0%}, +{prediction['threshold']:.0%} {probabilities['up_barrier']:.0%}. Distinct historical samples: {row['effective_samples']}."
                )
            else:
                lines.append(f"- {horizon} d: insufficient history for an estimate.")
        lines += [
            "",
            "Observed frequencies in comparable historical cases; an observed 0% does not mean impossible. Events may overlap. These estimates are not calibrated probabilities."
        ]
    health = bundle["source_health"]
    failures = [r for r in health["runs"] if r["status"] in ("error", "partial")]
    lines += [
        "",
        "## Coverage and limitations",
        "",
        f"- Price markets available for this asset: {len(bundle['market_coverage'])}.",
        f"- Sources never collected: {', '.join(health['never_collected']) or 'none'}.",
    ]
    for run in failures[:6]:
        detail = (
            "Some data is inaccessible."
            if run["status"] == "partial"
            else "Data was inaccessible during the last collection."
        )
        if any("451" in error for error in run["errors"]):
            detail = "Access restricted from the collection environment."
        lines.append(f"- {run['source']}: {detail}")
    for limit in [*analysis["limitations"], *bundle["limitations"]][:9]:
        lines.append(f"- {limit}")
    return "\n".join(lines) + "\n"
