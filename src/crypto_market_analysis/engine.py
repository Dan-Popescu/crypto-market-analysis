"""One read-only analytical interface shared by CLI and MCP."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .asset_universe import validate_asset
from .config import Settings
from .evidence import CapturedInputs, metric_identity
from .freshness import metric_freshness
from .http_limits import bounded_engine_http
from .models import Candle, Observation
from .research import build_analysis, daily_bars, evaluate, forecast
from .storage import Store


def stamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class Engine:
    def __init__(self, settings: Settings, mode: str = "live", *, initialize: bool = True):
        if mode not in ("live", "demo", "import"):
            raise ValueError("Invalid mode.")
        self.settings = settings
        self.dataset_lineage = None
        marker = settings.data_dir / "dataset.json"
        if mode == "import" and marker.is_file():
            from .history_dataset import DatasetConfig

            if marker.stat().st_size > 2_000_000:
                raise ValueError("Research dataset identity exceeds the 2 MB limit.")
            dataset = DatasetConfig(**json.loads(marker.read_text(encoding="utf-8")))
            if Path(dataset.directory).resolve() != settings.data_dir.resolve():
                raise ValueError("Research dataset directory does not match its identity.")
            self.dataset_lineage = {"name": dataset.name, "source": dataset.source,
                                    "data_mode": dataset.data_mode,
                                    "target_start": dataset.target_start, "target_end": dataset.target_end}
        self.store = Store(settings.database, initialize=initialize)
        from .storage_policy import StoragePolicy

        self.store.storage_policy = StoragePolicy.from_settings(settings)
        self.mode = mode

    @staticmethod
    def _select_market(candles: list[Candle], cutoff: int) -> tuple[list[Candle], list[dict]]:
        grouped = defaultdict(list)
        for candle in candles:
            grouped[(candle.source, candle.quote)].append(candle)
        coverage = [{"source": venue, "quote": currency, "hourly_candles": len(bars),
                     "complete_days": len(daily_bars(bars)),
                     "last_close_time": max(c.close_time for c in bars)}
                    for (venue, currency), bars in grouped.items()]
        coverage.sort(key=lambda row: (row["last_close_time"] >= cutoff - 7200,
                                      row["complete_days"], row["last_close_time"], row["source"]), reverse=True)
        if not coverage:
            return [], []
        selected = coverage[0]
        return grouped[(selected["source"], selected["quote"])], coverage

    def selected_candles(self, asset: str, source: str | None = None, quote: str | None = None,
                         as_of: int | None = None, watermark: dict | None = None) -> tuple[list[Candle], list[dict]]:
        asset = validate_asset(asset)
        now = int(time.time()) if as_of is None else as_of
        candles = self.store.candles(asset, source, quote, now, self.mode,
                                     max_id=watermark["candles"] if watermark else None)
        return self._select_market(candles, now)

    def _health(self, now: int, runs: list[dict], counts: dict) -> dict:
        for run in runs:
            run["age_seconds"] = now - run["observed_at"]
            periods = [{"liquidity": self.settings.liquidity_interval,
                        "market": self.settings.market_interval,
                        "options": self.settings.options_interval}.get(task, self.settings.context_interval)
                       for task in run["task"].split("+")]
            run["stale"] = run["age_seconds"] >= min(periods) * 2
            run["freshness_basis"] = "collection_completion; inspect individual market event age separately"
        observed = {run["source"] for run in runs}
        return {"as_of": stamp(now), "data_mode": self.mode, "counts": counts, "runs": runs,
                "configured_venues": list(self.settings.venues),
                "never_collected": [venue for venue in self.settings.venues if venue not in observed],
                "paid_collection_apis": False}

    def status(self, as_of: int | None = None) -> dict:
        now = int(time.time()) if as_of is None else as_of
        result = self._health(now, self.store.runs(now), self.store.counts())
        result["counts_scope"] = "all stored versions and modes; operational inventory"
        return result

    def capture_inputs(self, asset: str = "BTC", source: str | None = None,
                       quote: str | None = None, as_of: int | None = None, *,
                       profile: str = "bundle", vigilance_policy: dict | None = None,
                       include_candle_versions: bool = False) -> CapturedInputs:
        asset = validate_asset(asset)
        now = int(time.time()) if as_of is None else as_of
        raw = self.store.capture(asset, now, self.mode, source=source, quote=quote,
                                 profile=profile, vigilance_policy=vigilance_policy,
                                 include_candle_versions=include_candle_versions)
        candles, coverage = self._select_market(raw["candles"], now)
        observations = raw["observations"]
        if profile == "vigilance":
            observations = [row for row in observations if candles and row.quote == candles[0].quote
                            and (row.metric.startswith(("funding_rate_", "open_interest_"))
                                 or row.source == candles[0].source)]
            for family in raw["truncation"]["observation_coverage"]:
                retained = [row for row in observations if ("derivatives" if row.metric.startswith(
                    ("funding_rate_", "open_interest_")) else "liquidity") == family["family"]]
                family.update(captured=len(retained), omitted=family.get("available", 0) - len(retained),
                              required_available=len(retained), required_captured=len(retained),
                              truncated=family.get("available", 0) > len(retained),
                              captured_start=min((row.event_time for row in retained), default=None),
                              captured_end=max((row.event_time for row in retained), default=None))
            raw["truncation"]["observations"] = any(
                family.get("truncated", False) for family in raw["truncation"]["observation_coverage"])
        if lineage := getattr(self, "dataset_lineage", None):
            raw["capture_profile"]["dataset_lineage"] = deepcopy(lineage)
        configuration = asdict(self.settings)
        configuration.pop("data_dir", None)
        return CapturedInputs(now, asset, self.mode, candles[0].source if candles else source,
                              candles[0].quote if candles else quote, tuple(candles), tuple(observations),
                              tuple(coverage), tuple(raw["events"]),
                              self._health(now, raw["runs"], raw["counts"]) if profile == "bundle" else
                              {"status": "not_requested", "as_of": stamp(now), "data_mode": self.mode}, raw["watermark"],
                              configuration, raw["truncation"], tuple(raw["calendar"]), raw["capture_profile"],
                              tuple(raw["candle_versions"]) if raw["candle_versions"] is not None else None)

    def inputs(self, asset: str, source: str | None = None, quote: str | None = None,
               as_of: int | None = None) -> tuple[list[Candle], list[Observation], list[dict], dict]:
        captured = self.capture_inputs(asset, source, quote, as_of)
        return (list(captured.candles), [row for row in captured.observations if row.asset == asset],
                list(captured.coverage), dict(captured.watermark))

    def bundle(self, asset: str = "BTC", source: str | None = None, quote: str | None = None,
               as_of: int | None = None, include_forecast: bool = True, *,
               captured: CapturedInputs | None = None) -> dict:
        captured = captured or self.capture_inputs(asset, source, quote, as_of)
        now, candles, observations = captured.as_of, list(captured.candles), list(captured.observations)
        settings = Settings(**captured.configuration)
        latest = {}
        for observation in sorted(observations, key=lambda row: (row.event_time, row.observed_at), reverse=True):
            dimension = metric_identity(observation)
            if dimension not in latest:
                freshness = metric_freshness(observation, settings, now)
                latest[dimension] = {**asdict(observation), "age_seconds": freshness["age_seconds"],
                                     "stale": freshness["stale"], "freshness": freshness}
        reference = captured.reference
        from .source_catalog import interpret_context
        additional_context = interpret_context(observations, now)
        for point in additional_context["observations"]:
            observation = Observation(**{key: point[key] for key in Observation.__dataclass_fields__})
            point["freshness"] = metric_freshness(observation, settings, now)
        prediction = forecast(candles, as_of=now) if include_forecast else None
        price_freshness = None
        if candles:
            from .freshness import assess_freshness
            last = max(candles, key=lambda row: row.close_time)
            price_freshness = assess_freshness(last.close_time, last.observed_at, now, settings.market_interval)
            if prediction is not None:
                prediction["stale"] = price_freshness["stale"]
                prediction["freshness"] = price_freshness
                if not price_freshness["usable"]:
                    for estimate in prediction["horizons"].values():
                        estimate.update(status="stale", probabilities=None, confidence_intervals=None)
        return {"schema_version": "2.0", "snapshot_id": reference["content_hash"][:24],
                "input_reference": reference, "as_of": stamp(now), "as_of_epoch": now,
                "data_mode": captured.data_mode, "asset": captured.asset,
                "selected_market": {"source": captured.source, "quote": captured.quote} if candles else None,
                "market_selection_rule": "explicit filters, then recent usable complete history; not an execution venue",
                "market_coverage": list(captured.coverage), "analysis": build_analysis(candles, observations, now),
                "forecast": prediction, "price_freshness": price_freshness,
                "additional_context": additional_context,
                "latest_metrics": list(latest.values())[:150], "metrics_truncated": len(latest) > 150,
                "input_truncation": dict(captured.truncation), "events": list(captured.events),
                "calendar": list(captured.calendar),
                "source_health": captured.source_health,
                "limitations": [
                    "No implicit USD/USDT/USDC/EUR conversion or cross-venue price averaging.",
                    "The historical reference market is distinct from a simulated purchase venue.",
                    "Hourly-close anchors are not current executable purchase prices.",
                    "Calendar and ETF coverage may be manual or incomplete; missing data is unknown.",
                    "Unverified macro release schedules cannot establish historical availability."]}

    @property
    def accumulation(self):
        from .application import AccumulationService
        return AccumulationService(self.settings, self.store, self.mode, self.capture_inputs, self.dataset_lineage)

    def context(self, asset="BTC", source=None, quote=None, as_of=None, lookbacks=None):
        return self.accumulation.context(asset, source, quote, as_of, lookbacks)

    def purchase_windows(self, asset="BTC", source=None, quote=None, as_of=None, horizons=None, thresholds=None):
        return self.accumulation.purchase_windows(asset, source, quote, as_of, horizons, thresholds)

    def compare_plans(self, config, as_of=None, validation="retrospective"):
        return self.accumulation.compare_plans(config, as_of, validation)

    def accumulation_brief(self, asset="BTC", source=None, quote=None, as_of=None, plan_config=None):
        return self.accumulation.accumulation_brief(asset, source, quote, as_of, plan_config)

    def cost_sensitivity(self, config, cost_cases, as_of=None, validation="retrospective"):
        return self.accumulation.cost_sensitivity(config, cost_cases, as_of, validation)

    def journal_summary(self, as_of=None, limit=20):
        return self.accumulation.journal_summary(as_of, limit)

    def journal_scorecard(self, as_of=None, limit=20):
        return self.accumulation.journal_scorecard(as_of, limit)

    def replay_assessment(self, assessment_id):
        return self.accumulation.replay_assessment(assessment_id)

    def evaluate_plans(self, config, as_of=None, origins=None, validation="known_at",
                       held_out_start=None, policy_frozen_at=None):
        return self.accumulation.evaluate_plans(config, as_of, origins, validation, held_out_start, policy_frozen_at)

    def issue_assessment(self, idempotency_key, asset="BTC", source=None, quote=None, as_of=None, plan_config=None):
        return self.accumulation.issue_assessment(idempotency_key, asset, source, quote, as_of, plan_config)

    def assessment(self, assessment_id):
        return self.accumulation.assessment(assessment_id)

    def assessment_outcomes(self, assessment_id):
        return self.accumulation.assessment_outcomes(assessment_id)

    def observe_assessment(self, assessment_id, as_of=None):
        return self.accumulation.observe_assessment(assessment_id, as_of)

    def vigilance(self, asset="BTC", source=None, quote=None, as_of=None, horizon_hours=4, policy=None):
        return self.accumulation.vigilance(asset, source, quote, as_of, horizon_hours, policy)

    def record_vigilance(self, state_key, asset="BTC", source=None, quote=None, as_of=None, horizon_hours=4, policy=None):
        return self.accumulation.record_vigilance(state_key, asset, source, quote, as_of, horizon_hours, policy)

    def import_history(self, path, source, asset, quote, member=None, expected_sha256=None):
        return self.accumulation.import_history(path, source, asset, quote, member, expected_sha256)

    def import_calendar(self, path, kind="bls"):
        return self.accumulation.import_calendar(path, kind)

    @bounded_engine_http
    def collect_calendar(self):
        return self.accumulation.collect_calendar()

    @bounded_engine_http
    def capture_books(self, *, sources, assets, quotes, as_of=None, clients=None):
        """Explicit local network capture with the actual capture clock."""
        from .collectors import capture_books

        if self.mode != "live" or as_of is not None:
            raise ValueError("Book capture requires live mode without as_of.")
        result = capture_books(self.store, sources=sources, assets=assets, quotes=tuple(quotes), clients=clients)
        failed = any(row.get("errors") for row in result["reports"])
        status = "partial" if failed and result["inserted_books"] else "error" if failed else "ok"
        return {**result, "status": status}

    @bounded_engine_http
    def collect_official_context(self, *, venues=("kraken",), include=("fx", "calendar"),
                                 max_requests=12, as_of=None, client=None, clients=None):
        """Capture and persist bounded official references and sourced calendars."""
        from .calendars import collect_calendars
        from .official_context import OfficialHTTP, capture_official_context

        if self.mode != "live" or as_of is not None:
            raise ValueError("Official collection requires live mode without as_of.")
        if (not include or len(set(include)) != len(include)
                or set(include) - {"fx", "calendar", "stablecoins"}):
            raise ValueError("include requires distinct fx, calendar or stablecoins entries.")
        if type(max_requests) is not int or not 1 <= max_requests <= 32:
            raise ValueError("max_requests must be between 1 and 32.")
        # Calendar has three bounded public sources; reserve its request budget.
        context_include = tuple(item for item in include if item != "calendar")
        calendar_budget = 3 if "calendar" in include else 0
        if max_requests < calendar_budget + bool(context_include):
            raise ValueError("The request budget cannot cover the selected context families.")
        now = int(time.time())
        owned = client is None
        client = OfficialHTTP() if owned else client
        try:
            result = (capture_official_context(client=client, clients=clients,
                       venues=tuple(venues), include=context_include,
                       max_requests=max_requests - calendar_budget)
                      if context_include else {"status": "ok", "observed_at": now, "errors": [], "warnings": []})
            inserted = {"fx_reference": 0, "stablecoin_price": 0, "calendar": 0}
            if result.get("fx_reference"):
                inserted["fx_reference"] += self.store.save_local_record("fx_reference", result["fx_reference"])["inserted"]
            for record in result.get("stablecoin_prices", []):
                inserted["stablecoin_price"] += self.store.save_local_record("stablecoin_price", record)["inserted"]
            if "calendar" in include:
                calendar = collect_calendars(int(time.time()), client=client, include_ecb=True,
                                             receipt_clock=time.time)
                result["calendar"] = calendar
                for event in calendar.get("events", []):
                    inserted["calendar"] += self.store.save_local_record("calendar", event)["inserted"]
                result["errors"] = [*result.get("errors", []), *calendar.get("errors", [])]
            if result.get("errors"):
                result["status"] = "partial" if any(inserted.values()) else "error"
            return {**result, "inserted": inserted, "data_mode": "live", "include": list(include),
                    "max_logical_requests": max_requests,
                    "logical_requests": result.get("logical_requests", 0) + calendar_budget,
                    "budget_scope": "Adapter operations; catalogue internals may make multiple HTTP requests."}
        finally:
            if owned:
                client.close()

    def stored_derivatives(self, request, as_of=None):
        from .reporting import bounded_as_of, bounded_object
        from .stored_analysis import analyze_stored_derivatives

        return analyze_stored_derivatives(self.store, bounded_object(request), bounded_as_of(as_of))

    def stored_quotes(self, request, as_of=None):
        from .reporting import bounded_as_of, bounded_object
        from .stored_analysis import analyze_stored_quotes

        return analyze_stored_quotes(self.store, bounded_object(request), bounded_as_of(as_of))

    def stored_venues(self, request, as_of=None):
        from .reporting import bounded_as_of, bounded_object
        from .stored_analysis import analyze_stored_venues

        return analyze_stored_venues(self.store, bounded_object(request), bounded_as_of(as_of))

    def qualify_sources(self, request, as_of=None):
        from .qualification import qualify_sources
        from .reporting import bounded_as_of, bounded_object

        return qualify_sources(self.store, bounded_object(request), bounded_as_of(as_of))

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
        if rows:
            from .freshness import assess_freshness
            last = max(rows, key=lambda row: row.close_time)
            freshness = assess_freshness(last.close_time, last.observed_at, now, self.settings.market_interval)
            result.update(stale=freshness["stale"], freshness=freshness)
            if not freshness["usable"]:
                for estimate in result["horizons"].values():
                    estimate.update(status="stale", probabilities=None, confidence_intervals=None)
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
        *,
        captured: CapturedInputs | None = None,
    ) -> dict:
        rows = list(captured.candles) if captured else self.selected_candles(asset, source, quote, as_of)[0]
        alignment = max((c.close_time for c in rows), default=0) % 86400 // 3600
        if captured is not None and captured.candle_versions is not None:
            rows = list(captured.candle_versions)
        elif rows:
            rows = self.store.candles(asset, rows[0].source, rows[0].quote,
                                      captured.as_of if captured else as_of, self.mode, versions=True,
                                      max_id=captured.watermark["candles"] if captured else None)
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

        if kind in ("context", "windows"):
            from .charts import render_result_chart
            result = (self.context(asset, source, quote, as_of) if kind == "context"
                      else self.purchase_windows(asset, source, quote, as_of))
            return render_result_chart(kind, result, output)

        now = int(time.time()) if as_of is None else as_of
        captured = self.capture_inputs(asset, source, quote, now)
        candles = list(captured.candles)
        observations = [row for row in captured.observations if row.asset == asset]
        if source:
            observations = [o for o in observations if o.source == source]
        if quote:
            observations = [o for o in observations if o.quote == quote]
        evaluation = (
            self.evaluation(asset, horizon, 0.10, source, quote, now, validation, captured=captured)
            if kind == "calibration"
            else None
        )
        return render_chart(kind, candles, observations, output, now, evaluation=evaluation,
                            input_reference=captured.reference)


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
