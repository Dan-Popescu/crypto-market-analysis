"""Read-only instrument research and explicit native capability discovery."""

from __future__ import annotations

import time

from pydantic import StrictInt

from .reporting import bounded_object


def _cutoff(as_of):
    if as_of is None:
        return int(time.time())
    if type(as_of) is not int or not -62135596800 <= as_of <= 253402300799:
        raise ValueError("as_of must be integer UTC epoch seconds.")
    return as_of


def register_market_tools(server, engine, readonly, present=None):
    from .cross_asset import application

    register = present or server.tool(annotations=readonly)

    @register
    def get_market_capabilities() -> dict:
        """Discover cross-asset identity, calendars, price bases, research and source qualification. Implementation tests and declared imports do not establish public feed access, licensing, historical knowledge or financial edge. No network or writes."""
        from .cross_asset.providers import provider_catalog

        return {**application.market_capabilities(), "sources": provider_catalog()}

    @register
    def get_instrument_registry(request: dict | None = None, as_of: StrictInt | None = None) -> dict:
        """Resolve effective and known-at instrument/listing aliases or inspect universe membership. Tickers, providers, venues and currencies remain distinct; ambiguity requires an explicit listing. No initialization or collection."""
        return application.market_registry(engine.settings.database, bounded_object(request or {}), as_of=_cutoff(as_of))

    @register
    def get_market_calendar(request: dict, as_of: StrictInt | None = None) -> dict:
        """Inspect an explicit calendar ID/version and expected native intervals. Distinguishes known closures, not-yet-due publications, halts and unknown schedule coverage. UTC instants retain local session dates/timezones. No writes."""
        return application.market_calendar(engine.settings.database, bounded_object(request), as_of=_cutoff(as_of))

    @register
    def get_instrument_analysis(request: dict, as_of: StrictInt | None = None) -> dict:
        """Analyze one resolved series with explicit data_mode, currency, interval bounds and declared annualization. Includes source/calendar/price-basis metadata and coverage; negative values disable positive-price methods. Captured history is not historical point-in-time evidence."""
        return application.market_analysis(engine.settings.database, bounded_object(request), as_of=_cutoff(as_of))

    @register
    def simulate_instrument_plan(request: dict, as_of: StrictInt | None = None) -> dict:
        """Simulate a hypothetical native raw-price cash plan with explicit costs, split and dividend handling. Requires supported instrument form and known completed-bar fills; reference and futures prices cannot be cash-spot fills. Ledgerly remains actual holdings authority."""
        return application.market_plan(engine.settings.database, bounded_object(request), as_of=_cutoff(as_of))

    @register
    def get_instrument_series(request: dict, as_of: StrictInt | None = None) -> dict:
        """Inspect at most 512 retained native intervals and declared/actual receipt provenance for one explicit series and data_mode. Narrow start/end when history exceeds max_rows. No provider request, schema migration or missing-store creation."""
        from .cross_asset.queries import inspect_market_series

        return inspect_market_series(engine.settings.database, bounded_object(request), as_of=_cutoff(as_of))

    @register
    def get_market_snapshot_status(snapshot_name: str = "default") -> dict:
        """Inspect a safe named market-v1 snapshot manifest. Manifest presence alone does not verify its data or prove live freshness. No export, collection or writes."""
        from .cross_asset.queries import market_snapshot_status

        return market_snapshot_status(engine.settings, snapshot_name)

    @register
    def get_market_fleet(request: dict, snapshot_name: str = "default", as_of: StrictInt | None = None) -> dict:
        """Read verified immutable mixed-class analytical summaries for up to 1000 explicit native series. Keep listing, currency, unit, price basis and calendar versions separate; bounded retained work is not live provider acceptance or a full coherent dossier."""
        from .cross_asset.queries import market_fleet

        return market_fleet(engine.settings, snapshot_name, bounded_object(request), as_of=_cutoff(as_of))

    @register
    def get_instrument_outcome(request: dict, as_of: StrictInt | None = None) -> dict:
        """Inspect a frozen instrument forecast and matching later eligible outcome. Pending/missing/excluded outcomes remain explicit. No issuance, observation writes, fitting or performance claim."""
        from .cross_asset.prospective import evaluate_market_assessment

        return evaluate_market_assessment(engine.settings.database, bounded_object(request), as_of=_cutoff(as_of))

    @register
    def calculate_futures_value(request: dict, as_of: StrictInt | None = None) -> dict:
        """Value a caller-declared futures position under an explicit currency, multiplier, expiry, collateral and margin model. Negative native prices are permitted when supported; no trade, broker margin guarantee or cash-spot accounting."""
        from .cross_asset.contracts import value_futures_position
        from .cross_asset.queries import request_fields

        values = request_fields(bounded_object(request), {"contract", "position", "native_currency"},
                         ("contract", "position", "native_currency"))
        return value_futures_position(values["contract"], values["position"],
                                      native_currency=values["native_currency"], as_of=_cutoff(as_of))

    @register
    def build_futures_research_series(request: dict, as_of: StrictInt | None = None) -> dict:
        """Construct an explicitly declared point-in-time unadjusted continuous futures research series. Source contracts, roll decisions, units and known-at chain metadata remain pinned. Derived roll prices are not executable fills."""
        from .cross_asset.contracts import build_continuous_series

        return build_continuous_series(bounded_object(request), as_of=_cutoff(as_of))
