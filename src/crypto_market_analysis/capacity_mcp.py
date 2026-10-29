"""Read-only discovery of measured collection work and coherent local analyses."""

from __future__ import annotations

from pydantic import StrictInt

from .reporting import bounded_as_of, bounded_object


def register_capacity_tools(server, engine, readonly, present=None) -> None:
    register = present or server.tool(annotations=readonly)

    @register
    def get_http_accounting(source: str | None = None, task: str | None = None,
                            start: StrictInt | None = None, end: StrictInt | None = None,
                            limit: StrictInt = 1000, endpoint_limit: StrictInt = 100) -> dict:
        """Read bounded observed HTTP attempts, body bytes, failures and latency from retained runs in the configured mode. Adapter calls and unknown instrumentation are not HTTP counts. Endpoint IDs omit URLs, queries and credentials; wire/TLS overhead is unknown. No capture or initialization."""
        from .http_accounting import stored_http_accounting

        return stored_http_accounting(engine.settings.database, source=source, task=task, mode=engine.mode,
                                      start=start, end=end, limit=limit, endpoint_limit=endpoint_limit)

    @register
    def get_auto_repair_status(limit: StrictInt = 20) -> dict:
        """Read current bounded automatic-repair task state for the configured import dataset. State is mutable, not a historical reconstruction. Missing datasets stay missing; this never plans, captures, retries, recovers or repairs data."""
        from .automatic_repair import automatic_repair_status

        return automatic_repair_status(engine.settings.database, mode=engine.mode, limit=limit)

    @register
    def get_analysis_dossier(request: dict, as_of: StrictInt | None = None) -> dict:
        """Read context, explicit stored venue/derivative/quote sections and known-at calendar from one bounded SQLite read transaction. Supply explicit source/native EUR or USD per market and independent derivative quote. Cost assumptions are caller-declared. Missing data stays unknown; no collection, issuance or paths. Use get_request_schema for nested fields and _mcp_result for frozen detail pages."""
        from .analysis_dossier import build_analysis_dossier

        request = dict(bounded_object(request, "analysis dossier request"))
        request.setdefault("data_mode", engine.mode)
        return build_analysis_dossier(engine.store, request, bounded_as_of(as_of))

    @register
    def get_installation_qualification(request: dict, as_of: StrictInt | None = None) -> dict:
        """Inspect independent BTC/EUR, ETH/EUR, BTC/USD and ETH/USD qualification locally, including coverage, gaps, freshness and public-capture evidence. This MCP operation forbids refresh; missing stores stay missing. Recent-only APIs do not establish years of history or French purchase eligibility."""
        from .installation_qualification import run_installation_qualification

        request = dict(bounded_object(request, "installation qualification request"))
        if request.get("refresh", False) is not False:
            raise ValueError("MCP qualification is read-only; refresh requires an explicit local CLI operation.")
        request.setdefault("data_mode", engine.mode)
        if as_of is not None:
            cutoff = bounded_as_of(as_of)
            if request.get("as_of") is not None and request["as_of"] != cutoff:
                raise ValueError("as_of conflicts with the qualification request.")
            request["as_of"] = cutoff
        return run_installation_qualification(engine.settings, request)
