"""Explicit operating experiments and bounded multi-asset analytical reads."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

COMMANDS = frozenset({"collection-capacity", "operating-acceptance", "operating-health", "operating-costs",
                      "freeze-validation", "prospective-validation", "analytics-snapshot", "analytics-fleet"})



def publish_protocol(artifact, output):
    """Atomically publish a complete declaration without replacing an original."""
    from .evidence import canonical_json

    text = canonical_json(artifact) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="." + output.name + "-",
                                         dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError:
            if output.is_symlink() or output.stat().st_size > 2_000_000:
                raise ValueError("Cannot overwrite this frozen protocol artifact.") from None
            if json.loads(output.read_text(encoding="utf-8")) != artifact:
                raise ValueError("An existing frozen protocol cannot be overwritten.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register_scalability_commands(command):
    descriptions = {
        "collection-capacity": ("Inspect native market demand and collection limits without network or writes.",
                                "crypto-market-analysis collection-capacity --config examples/observer.toml"),
        "operating-acceptance": ("Inspect an operating session; --apply runs its finite public collection experiment.",
                                 "crypto-market-analysis operating-acceptance --request examples/operating-acceptance.json"),
        "operating-health": ("Read bounded native coverage, gaps and freshness from the configured store.",
                             "crypto-market-analysis operating-health --request examples/operating-health.json"),
        "operating-costs": ("Read measured per-source work; supplied prices are estimates, not invoices.",
                            "crypto-market-analysis operating-costs --request examples/operating-costs.json"),
        "freeze-validation": ("Freeze a declared evaluation protocol at the actual clock before eligible issuance.",
                              "crypto-market-analysis freeze-validation --request examples/validation-protocol.json --output protocol.json"),
        "prospective-validation": ("Evaluate a frozen protocol against original journal issues and later outcomes.",
                                   "crypto-market-analysis prospective-validation --request validation-request.json"),
        "analytics-snapshot": ("Inspect an immutable all-vintage snapshot; --apply exports a new named snapshot.",
                               "crypto-market-analysis analytics-snapshot --name research --apply"),
        "analytics-fleet": ("Aggregate up to 1000 explicit assets in a verified immutable analytical snapshot.",
                            "crypto-market-analysis analytics-fleet --name research --request examples/analytics-fleet.json"),
    }
    for name, (description, example) in descriptions.items():
        parser = command(name, description, example)
        parser.add_argument("--request", type=Path,
                            required=name in ("freeze-validation", "prospective-validation", "analytics-fleet"),
                            help="Bounded JSON file or - for stdin")
        parser.add_argument("--format", choices=("json",), default="json")
        parser.add_argument("--output", help="Report file; frozen protocols cannot replace a different existing artifact")
        if name in ("operating-acceptance", "analytics-snapshot"):
            parser.add_argument("--apply", action="store_true", help="Explicit finite local capture/export")
        if name in ("analytics-snapshot", "analytics-fleet"):
            parser.add_argument("--name", default="default", help="Safe name scoped to the configured data directory")


def _fields(request, allowed):
    if not isinstance(request, dict) or set(request) - set(allowed):
        raise ValueError("Unknown fields in operating request.")
    return dict(request)



def run_scalability_command(args, settings, request):
    from .analytical_store import analytical_fleet, snapshot_path, snapshot_status
    from .reporting import bounded_as_of
    from .storage import Store

    request = {} if request is None else request
    if args.command == "collection-capacity":
        from .collection_plan import collection_plan

        return collection_plan(settings, **_fields(request, {"tasks"}))
    if args.command == "operating-health":
        from .operating_acceptance import operating_health

        values = _fields(request, {"source", "limit", "cursor", "required_history_hours", "freshness_seconds",
                                   "max_gap_hours", "max_query_rows"})
        return operating_health(settings.database, args.mode, as_of=args.as_of, **values)
    if args.command == "operating-costs":
        from .cost_report import operating_cost_report

        values = _fields(request, {"source", "task", "start", "limit", "cursor", "prices"})
        return operating_cost_report(settings.database, args.mode, as_of=args.as_of, **values)
    if args.command == "operating-acceptance":
        from .operating_acceptance import run_operating_acceptance

        if args.as_of is not None:
            raise ValueError("Operating experiments use the actual clock; omit --as-of.")
        if request.get("refresh") and not args.apply:
            raise ValueError("Public collection requires --apply; inspection performs no refresh.")
        if args.apply and args.mode != "live":
            raise ValueError("Operating collection requires explicit live mode.")
        if args.mode != "live":
            return {"status": "selection_mismatch", "configured_mode": args.mode, "session_mode": "live",
                    "read_only": True, "network_requested": False, "manifest": None}
        return run_operating_acceptance(settings, {**request, "refresh": args.apply})
    if args.command == "freeze-validation":
        from .validation_protocol import freeze_validation_protocol, validate_validation_protocol

        if args.as_of is not None:
            raise ValueError("Protocol freezing records the actual clock; omit --as-of.")
        output = Path(args.output) if args.output else None
        if output is not None and output.exists():
            if output.is_symlink() or output.stat().st_size > 2_000_000:
                raise ValueError("Cannot replace this validation protocol artifact.")
            previous = validate_validation_protocol(json.loads(output.read_text(encoding="utf-8")))
            candidate = freeze_validation_protocol(request, frozen_at=previous["frozen_at"])
            if previous != candidate:
                raise ValueError("An existing frozen protocol cannot be replaced by a different declaration.")
            return previous
        return freeze_validation_protocol(request, frozen_at=int(time.time()))
    if args.command == "prospective-validation":
        from .prospective_validation import evaluate_validation_request

        return evaluate_validation_request(Store(settings.database, initialize=False), request, bounded_as_of(args.as_of))
    if args.command == "analytics-fleet":
        if args.as_of is not None:
            if request.get("as_of") is not None and request["as_of"] != args.as_of:
                raise ValueError("--as-of conflicts with the analytical request cutoff.")
            request = {**request, "as_of": args.as_of}
        return analytical_fleet(settings, args.name, request)
    if args.command == "analytics-snapshot":
        from .analytical_store import export_candle_snapshot

        values = _fields(request, {"batch_rows", "max_rows", "rows_per_file", "assets", "source", "quote", "mode",
                                     "start", "end", "max_bytes", "max_seconds"})
        if args.as_of is not None:
            raise ValueError("Snapshot export freezes the actual store; omit --as-of.")
        if not args.apply:
            return {**snapshot_status(settings, args.name), "applied": False, "export_options": values}
        manifest = export_candle_snapshot(Store(settings.database, initialize=False), snapshot_path(settings, args.name), **values)
        return {"status": "exported", "applied": True, "snapshot_name": args.name, "manifest": manifest}
    raise ValueError("Unsupported scalability command.")


def scalability_exit_code(command, result, *, apply=False):
    if command == "operating-acceptance" and apply:
        from .operating_acceptance import operating_acceptance_exit_code

        return operating_acceptance_exit_code(result)
    return 0
