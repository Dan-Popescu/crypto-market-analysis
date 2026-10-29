"""Read-only MCP, local STDIO/HTTP and authenticated remote resource server."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import anyio
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import StrictInt

from .config import Settings
from .engine import Engine
from .reporting import bounded_as_of, bounded_integers, bounded_object, bounded_origins, bounded_thresholds
from .venues import VENUES

GLOSSARY = {
    "open_interest": {
        "definition": "Derivative contracts still open. Every contract has a buyer and a seller.",
        "limit": "An increase does not establish spot demand; units depend on the venue.",
    },
    "funding": {
        "definition": "Periodic payment between holders of perpetual contract positions.",
        "limit": "Compare only identical periods; projected and realized values are distinct.",
    },
    "depth": {
        "definition": "Amount of displayed orders within a price band around the order book midpoint.",
        "limit": "Liquidity can disappear. Incomplete coverage gives a lower bound.",
    },
    "volatility": {
        "definition": "Magnitude of price changes; the 21-day realized measure is annualized using 365 days.",
        "limit": "It does not give the direction of the next move.",
    },
    "implied_volatility": {
        "definition": "Volatility consistent with quoted option prices.",
        "limit": "It also reflects risk premiums; it is not a calibrated real-world probability.",
    },
    "spot_pressure": {
        "definition": "Share of volume initiated by aggressive buyers trading against sellers.",
        "limit": "A venue-specific measure; every trade has a buyer and a seller.",
    },
    "brier": {
        "definition": "Mean squared error between predicted probability and observed outcome; lower is better.",
        "limit": "Compare with a baseline on exactly the same dates, without future information.",
    },
    "red": {
        "definition": "Endpoint price below the anchor hourly close, over 24-hour windows.",
        "limit": "Distinct from a red calendar candle. The current price may differ from the anchor.",
    },
}


class JWTVerifier:
    def __init__(self, issuer: str, jwks_url: str, audience: str, resource: str):
        self.issuer, self.audience, self.resource = issuer, audience, resource
        self.keys = jwt.PyJWKClient(jwks_url, cache_keys=True, timeout=5)

    async def verify_token(self, token: str) -> AccessToken | None:
        def verify():
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            scopes = claims.get("scope", "")
            if not isinstance(scopes, str):
                return None
            return AccessToken(
                token=token,
                client_id=str(claims.get("azp") or claims["sub"]),
                subject=str(claims["sub"]),
                scopes=scopes.split(),
                expires_at=int(claims["exp"]),
                resource=self.resource,
            )

        try:
            return await anyio.to_thread.run_sync(verify)
        except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError):
            return None


def create_server(
    settings: Settings, mode: str = "live", host: str = "127.0.0.1", port: int = 8000
) -> FastMCP:
    issuer = os.getenv("OBSERVER_OAUTH_ISSUER")
    jwks = os.getenv("OBSERVER_JWKS_URL")
    public_url = os.getenv("OBSERVER_PUBLIC_URL")
    auth = verifier = None
    if any((issuer, jwks, public_url)):
        if not all((issuer, jwks, public_url)):
            raise ValueError(
                "OAuth requires OBSERVER_OAUTH_ISSUER, OBSERVER_JWKS_URL and OBSERVER_PUBLIC_URL."
            )
        if any(urlparse(v).scheme != "https" or not urlparse(v).hostname for v in (issuer, jwks, public_url)):
            raise ValueError("OAuth and public URLs must use HTTPS.")
        if not public_url.rstrip("/").endswith("/mcp"):
            raise ValueError("OBSERVER_PUBLIC_URL must be the full HTTPS URL ending in /mcp.")
        audience = os.getenv("OBSERVER_OAUTH_AUDIENCE", public_url)
        verifier = JWTVerifier(issuer, jwks, audience, public_url)
        auth = AuthSettings(
            issuer_url=issuer,
            resource_server_url=public_url,
            required_scopes=["observer:read"],
            validate_token_resource=True,
        )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("A public network listener requires OAuth configuration; otherwise use 127.0.0.1.")
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if public_url:
        allowed_hosts.append(urlparse(public_url).netloc)
    server = FastMCP(
        "Crypto Market Analysis",
        host=host,
        port=port,
        auth=auth,
        token_verifier=verifier,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts
        ),
        instructions="Analyze BTC/ETH over 1–60 days. For accumulation use get_accumulation_brief with an explicitly pinned source/native EUR or USD quote or matching plan. It combines descriptive context through 730 days, experimental 1/3/7/14-day purchase windows and hypothetical plans assessed at 90/180 days on one snapshot. Use compare_cost_sensitivity for caller-declared costs and get_journal_summary for bounded pending/missing cohorts. get_journal_scorecard adds descriptive outcomes with independent sample selection; replay_assessment verifies frozen original calculations without observing outcomes. preview_shared_budget describes declared shared cash constraints. calculate_purchase_costs, calculate_venue_disagreement, get_quote_context and get_derivatives_context use explicit caller-supplied local observations without collection. All tools are read-only; assessment issuance and outcome observation require explicit local CLI operations. get_analysis_bundle provides the broader source dossier. Percentages are experimental; never describe them as calibrated or as a demonstrated edge. Missing data is not a neutral signal. Cite the source, unit and anchor. Keep USD, EUR and stablecoins separate, and distinguish demonstration data from real data. Use render_chart to illustrate observations. Explain contradictions and the conditions that would change the interpretation. No order execution.",
    )
    engine = Engine(settings, mode)
    readonly = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @server.tool(annotations=readonly)
    def get_analysis_bundle(
        asset: Literal["BTC", "ETH"] = "BTC",
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
    ) -> dict[str, Any]:
        """Use first for a concise research dossier: observations, scenarios, coverage, sources and limits. as_of is UTC epoch seconds; never invent missing evidence."""
        return engine.bundle(asset, source, quote, as_of)

    @server.tool(annotations=readonly)
    def get_accumulation_brief(
        asset: Literal["BTC", "ETH"] = "BTC",
        source: str | None = None,
        quote: Literal["EUR", "USD"] | None = None,
        as_of: int | None = None,
        plan_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One pinned-snapshot next-tranche review: observations, funding, targets/deadlines and pending outcomes. Supply explicit source/native quote or a matching plan object. No buy score or policy selection."""
        return engine.accumulation_brief(asset, source, quote, as_of,
            bounded_object(plan_config) if plan_config is not None else None)

    @server.tool(annotations=readonly)
    def construct_purchase_plan(request: dict[str, Any]) -> dict[str, Any]:
        """Pure UTC recurrence constructor returning schema-1 JSON without saving it. Amounts are decimal strings; monthly schedules require an explicit clip/skip month-end convention. No personal holdings required."""
        from .plan_builder import construct_plan
        return construct_plan(bounded_object(request, "plan request"))

    @server.tool(annotations=readonly)
    def preview_purchase_funding(config: dict[str, Any], as_of: int | None = None) -> dict[str, Any]:
        """Price-free funding constraints with fees, future deposits and free/locked commitments. This is a conservative declared-plan illustration, not an account balance or fill simulation."""
        from .plan_builder import preview_plan_funding
        return preview_plan_funding(bounded_object(config), int(time.time()) if as_of is None else as_of)

    @server.tool(annotations=readonly)
    def compare_cost_sensitivity(
        config: dict[str, Any], cost_cases: list[dict[str, Any]], as_of: int | None = None,
        validation: Literal["known_at", "retrospective"] = "retrospective",
    ) -> dict[str, Any]:
        """Read-only comparison of 1–3 caller-named fee/spread/slippage cases with frozen policy/cashflows/bars. First case is an arithmetic reference, never a preferred case. No optimization or measured-fill claim."""
        bounded_object({"cases": cost_cases}, "cost cases")
        return engine.cost_sensitivity(bounded_object(config), cost_cases, as_of, validation)

    @server.tool(annotations=readonly)
    def get_journal_summary(as_of: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Bounded read-only issued-event cohorts with pending/missing/excluded denominators and first definite outcomes. Modes/quotes/lineages stay separate; no independence or performance score is claimed. Up to 100 originals."""
        return engine.journal_summary(as_of, limit)

    @server.tool(annotations=readonly)
    def get_journal_scorecard(as_of: StrictInt | None = None, limit: StrictInt = 20) -> dict[str, Any]:
        """Bounded descriptive journal scorecard with pending/missing counts, independent scenario selection and uncertainty. Quotes, modes, rules and chronology stay separate; no calibrated forecast or proven advantage. Up to 100 originals."""
        return engine.journal_scorecard(bounded_as_of(as_of), limit)

    @server.tool(annotations=readonly)
    def preview_shared_budget(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Price-free preview of 2–16 declared plans sharing one explicit native-quote cash envelope. Supply a bounded JSON object, never a path. Declared contributions and commitments are hypothetical; this is not an account ledger."""
        from .budget import preview_budget_envelope

        return preview_budget_envelope(bounded_object(request, "budget request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def calculate_purchase_costs(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Read-only purchase arithmetic from caller-supplied native-quote ask ladders, explicit fees and product rules. Missing/stale/skewed venues remain visible. No collection, execution, implicit currency conversion or preferred venue."""
        from .venue_analysis import analyze_purchase_costs

        return analyze_purchase_costs(bounded_object(request, "purchase cost request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def calculate_venue_disagreement(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Describe same-basis, contemporaneous native-quote prices supplied as a bounded JSON object. Show coverage, dispersion and timing exclusions; disagreement alone is not an arbitrage or purchase signal."""
        from .venue_analysis import analyze_venue_disagreement

        return analyze_venue_disagreement(bounded_object(request, "venue request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def get_quote_context(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Describe explicitly sourced indicative EUR/USD and stablecoin basis observations supplied as JSON. Native observations, conversion lineage and endpoint timing remain visible; indicative FX is not an executable rate."""
        from .quote_context import analyze_quote_context

        return analyze_quote_context(bounded_object(request, "quote context request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def replay_assessment(assessment_id: str) -> dict[str, Any]:
        """Verify an existing issued assessment's original calculations from its frozen local evidence. Accepts only an assessment ID; no file paths, collection, issuance, future outcomes or journal append."""
        return engine.replay_assessment(assessment_id)

    @server.tool(annotations=readonly)
    def get_derivatives_context(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Describe fixed 4-hour/24-hour changes from caller-supplied derivatives observations. Native quotes, contracts, units, funding periods and known-at endpoints stay separate; missing endpoints remain unknown. No collection, annualization or directional forecast."""
        from .derivatives_context import analyze_derivatives_context

        return analyze_derivatives_context(bounded_object(request, "derivatives request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def get_stored_derivatives(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Read bounded SQLite derivatives known at the cutoff. JSON requires asset, source, native quote and data_mode; optional policy controls endpoint age/skew. No network or paths. Native contract units remain separate; missing endpoints stay unknown."""
        return engine.stored_derivatives(bounded_object(request, "stored derivatives request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def get_stored_quotes(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Read bounded local FX/stablecoin references selected by explicit sources, native quote and data_mode JSON. Optional max_age_seconds and max_endpoint_skew_seconds bound eligibility. No paths, network or executable conversion; missing clocks remain unknown."""
        return engine.stored_quotes(bounded_object(request, "stored quotes request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def get_stored_venue_costs(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Read retained local ask ladders for explicit sources, asset, native quote and data_mode. Supply budget and assumptions_by_source with declared fees/product rules. Unknown fees remain unknown; no collection, paths, currency conversion or preferred venue."""
        request = dict(bounded_object(request, "stored venue costs request"))
        if request.get("analysis", "purchase_costs") != "purchase_costs":
            raise ValueError("analysis must be purchase_costs for this tool.")
        request["analysis"] = "purchase_costs"
        return engine.stored_venues(request, bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def get_stored_venue_disagreement(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Describe bounded local snapshots for explicit sources, asset, native quote and data_mode JSON. Timing, missing venues, capture truncation and provenance stay visible. No paths, network, implicit conversion or arbitrage claim."""
        request = dict(bounded_object(request, "stored venue disagreement request"))
        if request.get("analysis", "disagreement") != "disagreement":
            raise ValueError("analysis must be disagreement for this tool.")
        request["analysis"] = "disagreement"
        return engine.stored_venues(request, bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def qualify_sources(request: dict[str, Any], as_of: StrictInt | None = None) -> dict[str, Any]:
        """Read-only offline qualification of bounded local native-market history and declared resources. JSON includes sources, optional markets/market_sources, data_mode, required_history_days and resource ceilings. Show gaps, freshness, access/rights/eligibility and qualification gates independently. No paths, collection, issuance or completeness promise."""
        return engine.qualify_sources(bounded_object(request, "qualification request"), bounded_as_of(as_of))

    @server.tool(annotations=readonly)
    def get_market_context(
        asset: Literal["BTC", "ETH"] = "BTC",
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        lookbacks: list[int] | None = None,
    ) -> dict[str, Any]:
        """Descriptive native-quote historical context, complete UTC calendar periods and coverage. Up to 16 lookbacks of 1–3650 days; low historical rank is not undervaluation."""
        return engine.context(asset, source, quote, as_of, bounded_integers(lookbacks, "lookbacks", 3650, 16))

    @server.tool(annotations=readonly)
    def get_purchase_windows(
        asset: Literal["BTC", "ETH"] = "BTC",
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        horizons: list[int] | None = None,
        thresholds: list[float] | None = None,
    ) -> dict[str, Any]:
        """Experimental endpoint and below-anchor touch frequencies, reference rates, uncertainty and expiry. Defaults: 1/3/7/14 days; up to 16 horizons and 12 decline fractions. No fill or calibrated purchase signal is implied."""
        horizons = bounded_integers(horizons, "horizons", 365, 16)
        thresholds = bounded_thresholds(thresholds)
        return engine.purchase_windows(asset, source, quote, as_of, horizons, thresholds)

    @server.tool(annotations=readonly)
    def compare_purchase_plans(
        config: dict[str, Any],
        as_of: int | None = None,
        validation: Literal["known_at", "retrospective"] = "retrospective",
    ) -> dict[str, Any]:
        """Read-only regular/split/deferred purchase simulation, including reserve, fees, holdings plus reserve and pending 90/180-day outcomes. Supply a bounded JSON object, never a path. Money uses decimal strings."""
        return engine.compare_plans(bounded_object(config), as_of=as_of, validation=validation)

    @server.tool(annotations=readonly)
    def evaluate_purchase_plans(
        config: dict[str, Any],
        as_of: int | None = None,
        origins: list[int] | None = None,
        validation: Literal["known_at", "retrospective"] = "known_at",
        held_out_start: int | None = None,
        policy_frozen_at: int | None = None,
    ) -> dict[str, Any]:
        """Read-only paired multi-origin evaluation with baseline underperformance, reserve and contribution-adjusted drawdown. Up to 24 UTC origin timestamps; strict empty availability remains empty."""
        return engine.evaluate_plans(
            bounded_object(config),
            as_of=as_of,
            origins=bounded_origins(origins),
            validation=validation,
            held_out_start=held_out_start,
            policy_frozen_at=policy_frozen_at,
        )

    @server.tool(annotations=readonly)
    def get_assessment(assessment_id: str) -> dict[str, Any]:
        """Read an immutable previously issued local assessment and its appended outcomes. This lookup does not issue assessments or append observations."""
        if not 1 <= len(assessment_id) <= 200:
            raise ValueError("assessment_id must contain 1–200 characters.")
        original = engine.assessment(assessment_id)
        evidence = original.pop("evidence", {})
        outcomes = engine.assessment_outcomes(assessment_id)
        return {**original, "evidence_summary": {"available_locally": True,
                "candles": evidence.get("candles_count", len(evidence.get("candles", []))),
                "observations": evidence.get("observations_count", len(evidence.get("observations", []))),
                "content_hash": original["content_hash"]},
                "outcomes": outcomes[-20:], "outcomes_truncated": len(outcomes) > 20}

    @server.tool(annotations=readonly)
    def get_vigilance(
        asset: Literal["BTC", "ETH"] = "BTC",
        source: str | None = None,
        quote: str | None = None,
        as_of: int | None = None,
        horizon_hours: Literal[4, 8] = 4,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read-only exceptional 4/8-hour observed fragility, reasons, contradictions, missing evidence and expiry. No persistent watch, notification, state transition or crash probability is created."""
        return engine.vigilance(
            asset,
            source,
            quote,
            as_of,
            horizon_hours,
            bounded_object(policy, "policy") if policy is not None else None,
        )

    @server.tool(annotations=readonly)
    def get_source_catalog() -> dict[str, Any]:
        """Documented source access, native quotes, rights and dated French entity evidence; not a live availability check."""
        from .source_catalog import source_catalog
        return source_catalog()

    @server.tool(annotations=readonly)
    def get_source_status() -> dict[str, Any]:
        """Use to check collection failures, stale data, configured venue coverage and entity-specific regulatory unknowns."""
        return {**engine.status(), "venues": [asdict(v) for v in VENUES.values()]}

    @server.tool(annotations=readonly)
    def get_forecast(
        asset: Literal["BTC", "ETH"] = "BTC",
        horizon: int = 7,
        threshold: float = 0.10,
        source: str | None = None,
        quote: str | None = None,
    ) -> dict[str, Any]:
        """Experimental historical frequencies at 1–60 days: endpoint up/down, intraperiod up/down barriers. Not calibrated probabilities; include hourly anchor, 24-hour window alignment and sample size."""
        if not 1 <= horizon <= 60:
            raise ValueError("Horizon must be between 1 and 60 days.")
        return engine.predictions(asset, [horizon], threshold, source, quote)

    @server.tool(annotations=readonly)
    def get_evaluation(
        asset: Literal["BTC", "ETH"] = "BTC",
        horizon: int = 7,
        source: str | None = None,
        quote: str | None = None,
        validation: Literal["known_at", "retrospective"] = "known_at",
    ) -> dict[str, Any]:
        """Chronological, non-overlapping evaluation against an unconditional base, with Brier and observed calibration. No claim of profitability."""
        if not 1 <= horizon <= 60:
            raise ValueError("Horizon must be between 1 and 60 days.")
        result = engine.evaluation(asset, horizon, 0.10, source, quote, validation=validation)
        return {
            **result,
            "origins": result["origins"][-20:],
            "origins_truncated": len(result["origins"]) > 20,
        }

    @server.tool(annotations=readonly)
    def get_metric_history(
        asset: Literal["BTC", "ETH", "MACRO", "STABLECOINS"],
        metric: str,
        source: str | None = None,
        limit: StrictInt = 100,
        quote: str | None = None,
        unit: str | None = None,
        dimension: dict[str, Any] | None = None,
        as_of: StrictInt | None = None,
        start: StrictInt | None = None,
        end: StrictInt | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read known-at native metric history, up to 500 points per page. Pin source, quote, unit and complete dimension to select one series; mixed series are explicitly identified. Repeat selectors with next_cursor to continue a cutoff/watermark-pinned view. Default coverage is 365 days; explicit start/end are UTC seconds. Retention may invalidate a cursor. No currency conversion or collection."""
        return engine.store.metric_history(asset, metric, source, limit, mode=mode, quote=quote,
                                           unit=unit, dimension=dimension, as_of=as_of,
                                           start=start, end=end, cursor=cursor)

    @server.tool(annotations=readonly)
    def explain_metric(metric: str) -> dict[str, Any]:
        """Use for plain English definitions and interpretation limits. Without a matching key, returns available glossary keys."""
        return GLOSSARY.get(metric, {"available_metrics": list(GLOSSARY), "unknown_metric": metric})

    @server.tool(annotations=readonly)
    def render_chart(
        asset: Literal["BTC", "ETH"] = "BTC",
        kind: Literal[
            "price",
            "weekly",
            "liquidity",
            "leverage",
            "options",
            "calibration",
            "context",
            "windows",
            "plans",
        ] = "weekly",
        source: str | None = None,
        quote: str | None = None,
        validation: Literal["known_at", "retrospective"] = "known_at",
        as_of: int | None = None,
        plan_config: dict[str, Any] | None = None,
    ) -> Image:
        """Create a sourced PNG from observed data: weekly candles, daily price, liquidity, leverage, options or empirical calibration. Missing data returns an error, never a simulated chart."""
        # Temporary output is returned as native MCP image content, no public file hosting needed.
        with tempfile.TemporaryDirectory(prefix="crypto-market-analysis-") as folder:
            if kind == "plans":
                from .charts import render_result_chart

                if plan_config is None:
                    raise ValueError("A plans chart requires plan_config.")
                result = engine.compare_plans(bounded_object(plan_config), as_of=as_of, validation=validation)
                path = render_result_chart("plans", result, Path(folder) / "chart.png")
            else:
                path = engine.chart(
                    kind,
                    asset,
                    Path(folder) / "chart.png",
                    source,
                    quote,
                    as_of if as_of is not None else int(time.time()),
                    validation=validation,
                )
            return Image(data=path.read_bytes(), format="png")

    return server
