"""Read-only operating evidence, frozen validation and fleet analytics."""

from __future__ import annotations

from pydantic import StrictInt

from .reporting import bounded_as_of, bounded_object


def register_scalability_tools(server, engine, readonly, present=None):
    register = present or server.tool(annotations=readonly)

    @register
    def get_collection_capacity(tasks: list[str] | None = None) -> dict:
        """Inspect configured asset/source/quote/cadence demand and collection limits. Counts are requested work, not provider support or measured HTTP. No collection or schema initialization."""
        from .collection_plan import collection_plan

        return collection_plan(engine.settings, **({"tasks": tasks} if tasks is not None else {}))

    @register
    def get_operating_health(request: dict | None = None, as_of: StrictInt | None = None) -> dict:
        """Read bounded retained native-hour coverage, gaps, freshness and receipt lag. Cursor pages pin their declared selection; unavailable evidence remains unknown. No refresh or writes."""
        from .operating_acceptance import operating_health

        return operating_health(engine.settings.database, engine.mode, as_of=as_of,
                                **bounded_object(request or {}, "operating health request"))

    @register
    def get_operating_costs(request: dict | None = None, as_of: StrictInt | None = None) -> dict:
        """Read retained source/task HTTP attempts, decoded bytes, latency and failures. Unknown transport work is not zero; declared prices give estimates, not bills. No network or writes."""
        from .cost_report import operating_cost_report

        return operating_cost_report(engine.settings.database, engine.mode, as_of=as_of,
                                     **bounded_object(request or {}, "operating cost request"))

    @register
    def get_operating_acceptance(request: dict | None = None) -> dict:
        """Inspect one configured operating experiment and its actual elapsed observation. This MCP operation rejects refresh; finite capture requires the explicit local CLI. Missing sessions stay missing."""
        from .operating_acceptance import run_operating_acceptance

        values = bounded_object(request or {}, "operating acceptance request")
        if values.get("refresh") not in (None, False):
            raise ValueError("MCP operating acceptance is read-only; refresh is forbidden.")
        if engine.mode != "live":
            return {"status": "selection_mismatch", "configured_mode": engine.mode, "session_mode": "live",
                    "read_only": True, "network_requested": False, "manifest": None}
        return run_operating_acceptance(engine.settings, {**values, "refresh": False})

    @register
    def get_prospective_validation(request: dict, as_of: StrictInt | None = None) -> dict:
        """Evaluate frozen protocol cohorts against immutable issues and later eligible observations. Preserve pending, missing, excluded and overlapping denominators. No issuance, observation, fitting or calibrated-edge claim."""
        from .prospective_validation import evaluate_validation_request

        return evaluate_validation_request(engine.store, bounded_object(request), bounded_as_of(as_of))

    @register
    def get_analytical_snapshot_status(snapshot_name: str = "default") -> dict:
        """Inspect a named candle snapshot manifest inside the configured directory. Manifest inspection does not verify data integrity or current live coverage; no export or writes."""
        from .analytical_store import snapshot_status

        return snapshot_status(engine.settings, snapshot_name)

    @register
    def get_analytical_fleet(request: dict, snapshot_name: str = "default") -> dict:
        """Read a verified immutable all-vintage candle snapshot using bounded DuckDB aggregation for up to 1000 explicit assets. Native source, quote, mode and known-at cutoff stay separate. Snapshot coverage is not live freshness or a coherent full dossier."""
        from .analytical_store import analytical_fleet

        return analytical_fleet(engine.settings, snapshot_name, bounded_object(request))
