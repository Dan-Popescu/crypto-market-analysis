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
        "definition": "Contrats dérivés encore ouverts. Chaque contrat a un acheteur et un vendeur.",
        "limit": "Une hausse ne prouve pas une demande spot ; les unités dépendent de la plateforme.",
    },
    "funding": {
        "definition": "Paiement périodique entre détenteurs de positions sur contrats perpétuels.",
        "limit": "Comparer uniquement des périodes identiques ; projeté et réalisé sont distincts.",
    },
    "depth": {
        "definition": "Montant des ordres affichés dans une bande de prix autour du milieu du carnet.",
        "limit": "La liquidité peut disparaître. Une couverture incomplète donne une borne inférieure.",
    },
    "volatility": {
        "definition": "Ampleur des variations ; la mesure réalisée sur 21 jours est annualisée avec 365 jours.",
        "limit": "Elle ne donne pas la direction du prochain mouvement.",
    },
    "implied_volatility": {
        "definition": "Volatilité compatible avec les prix cotés des options.",
        "limit": "Elle reflète aussi les primes de risque ; ce n'est pas une probabilité réelle calibrée.",
    },
    "spot_pressure": {
        "definition": "Part du volume déclenchée par des achats agressifs contre des vendeurs.",
        "limit": "Mesure locale à une plateforme ; toute transaction a un acheteur et un vendeur.",
    },
    "brier": {
        "definition": "Erreur quadratique moyenne entre probabilité prévue et événement observé ; plus petit est meilleur.",
        "limit": "Comparer à une référence sur exactement les mêmes dates, sans information future.",
    },
    "red": {
        "definition": "Cours à l'échéance inférieur à la clôture horaire servant d'ancrage, sur des fenêtres de 24 heures.",
        "limit": "Distinct d'une bougie calendaire rouge. Le cours instantané peut différer de l'ancrage.",
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
            raise ValueError("OAuth exige OBSERVER_OAUTH_ISSUER, OBSERVER_JWKS_URL et OBSERVER_PUBLIC_URL.")
        if any(urlparse(v).scheme != "https" or not urlparse(v).hostname for v in (issuer, jwks, public_url)):
            raise ValueError("Les URL OAuth et publique doivent être en HTTPS.")
        if not public_url.rstrip("/").endswith("/mcp"):
            raise ValueError("OBSERVER_PUBLIC_URL doit être l'URL HTTPS complète terminant par /mcp.")
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
            "Une écoute réseau publique exige la configuration OAuth ; sinon utilisez 127.0.0.1."
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
        instructions="Analyse BTC/ETH à 1–60 jours. Commencez par get_analysis_bundle pour connaître date, mode, couverture et sources. Les pourcentages sont expérimentaux ; ne les décrivez jamais comme calibrés ou comme un avantage démontré. Un manque de données n'est pas un signal neutre. Citez la source, l'unité et l'ancrage. Ne mélangez pas USD, EUR et stablecoins, ni démonstration et données réelles. Utilisez render_chart pour illustrer les observations. Expliquez clairement les contradictions et les conditions qui changeraient l'interprétation. Aucun passage d'ordre.",
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
            raise ValueError("Horizon entre 1 et 60 jours.")
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
            raise ValueError("Horizon entre 1 et 60 jours.")
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
            raise ValueError("limit doit être entre 1 et 500.")
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
        """Use for plain French definitions and interpretation limits. Without a matching key, returns available glossary keys."""
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
