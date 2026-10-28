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

from .config import Settings
from .engine import Engine
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
            raise ValueError("OAuth requires OBSERVER_OAUTH_ISSUER, OBSERVER_JWKS_URL and OBSERVER_PUBLIC_URL.")
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
        raise ValueError(
            "A public network listener requires OAuth configuration; otherwise use 127.0.0.1."
        )
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
        instructions="Analyze BTC/ETH over 1–60 days. Start with get_analysis_bundle to establish the date, mode, coverage and sources. Percentages are experimental; never describe them as calibrated or as a demonstrated edge. Missing data is not a neutral signal. Cite the source, unit and anchor. Keep USD, EUR and stablecoins separate, and distinguish demonstration data from real data. Use render_chart to illustrate observations. Explain contradictions and the conditions that would change the interpretation. No order execution.",
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
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read up to 500 observed points with source, unit, native quote, timestamps and coverage metadata. No currency conversion."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        rows = [
            o
            for o in engine.store.observations(asset, mode=mode)
            if o.metric == metric and (source is None or o.source == source)
        ]
        return {
            "asset": asset,
            "metric": metric,
            "data_mode": mode,
            "points": [asdict(o) for o in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    @server.tool(annotations=readonly)
    def explain_metric(metric: str) -> dict[str, Any]:
        """Use for plain English definitions and interpretation limits. Without a matching key, returns available glossary keys."""
        return GLOSSARY.get(metric, {"available_metrics": list(GLOSSARY), "unknown_metric": metric})

    @server.tool(annotations=readonly)
    def render_chart(
        asset: Literal["BTC", "ETH"] = "BTC",
        kind: Literal["price", "weekly", "liquidity", "leverage", "options", "calibration"] = "weekly",
        source: str | None = None,
        quote: str | None = None,
        validation: Literal["known_at", "retrospective"] = "known_at",
    ) -> Image:
        """Create a sourced PNG from observed data: weekly candles, daily price, liquidity, leverage, options or empirical calibration. Missing data returns an error, never a simulated chart."""
        # Temporary output is returned as native MCP image content, no public file hosting needed.
        with tempfile.TemporaryDirectory(prefix="crypto-market-analysis-") as folder:
            path = engine.chart(
                kind,
                asset,
                Path(folder) / "chart.png",
                source,
                quote,
                int(time.time()),
                validation=validation,
            )
            return Image(data=path.read_bytes(), format="png")

    return server
