"""Read-only access to changes, evidence readiness, robustness and backfill state."""

from __future__ import annotations

from pydantic import StrictInt

from .reporting import bounded_as_of, bounded_object


def register_analysis_tools(server, engine, readonly, present=None):
    register = present or server.tool(annotations=readonly)

    @register
    def get_analysis_changes(request: dict, as_of: StrictInt | None = None) -> dict:
        """Compare one explicit native dossier at two known-at cutoffs in one physical snapshot. Distinguish new market events, revisions, late/receipt-only data, clock expiry and source errors. Set previous_as_of and dossier; request schemas give nested examples. No collection or mutation; full changes use frozen result paging."""
        from .analysis_changes import build_analysis_changes

        return build_analysis_changes(engine.store, bounded_object(request),
                                      bounded_as_of(as_of))

    @register
    def get_analysis_readiness(request: dict, as_of: StrictInt | None = None) -> dict:
        """Diagnose specific supported claims and missing/stale/contradictory inputs for an explicit native dossier. Optional windows reuse actual experimental estimators. Data support, real-source acceptance, reuse rights and France eligibility stay separate. Next actions are guidance only; no collection, repair or issuance."""
        from .analysis_readiness import build_analysis_readiness

        return build_analysis_readiness(engine.store, bounded_object(request),
                                        bounded_as_of(as_of))

    @register
    def get_plan_robustness(request: dict, as_of: StrictInt | None = None) -> dict:
        """Compare fixed plan origins, caller-declared fees/execution and optional missing-hour stresses against regular purchases. Qualified completed comparisons remain separate from excluded/pending cells. Descriptive stability is not statistical confidence or a recommended policy. One stored read snapshot; no optimization, collection or writes."""
        from .robustness import build_plan_robustness

        return build_plan_robustness(engine.store, bounded_object(request), bounded_as_of(as_of))

    @register
    def get_history_backfill_status(limit: StrictInt = 20) -> dict:
        """Read current bounded Coinbase/Bitstamp backfill jobs in the configured import store. Checkpoints are mutable current state, not historical reconstruction. Missing stores remain missing; this never plans, captures, resumes, retries or migrates a database. Named Kraken datasets use get_auto_repair_status."""
        from .history_backfill import history_backfill_status

        return history_backfill_status(engine.settings.database, mode=engine.mode, limit=limit)
