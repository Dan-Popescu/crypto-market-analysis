"""Explicit cross-asset local operations; analytical reads never initialize."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .cross_asset.queries import (
    inspect_market_series,
    market_fleet,
    market_snapshot_path,
    market_snapshot_status,
)
from .cross_asset.queries import (
    request_fields as _fields,
)

COMMANDS = frozenset({
    "market-capabilities", "market-registry", "market-calendar", "market-calendar-build",
    "market-analysis", "market-plan", "market-import", "market-fetch", "market-snapshot",
    "market-fleet", "market-freeze", "market-issue", "market-outcome",
    "market-futures-value", "market-futures-curve", "market-series",
})
WRITERS = frozenset({"market-import", "market-fetch", "market-snapshot", "market-issue"})


def register_market_commands(command):
    descriptions = {
        "market-capabilities": "Inspect instrument capabilities, source qualification and explicit limitations.",
        "market-registry": "Resolve known/effective instruments, listings and aliases without collection.",
        "market-calendar": "Inspect a sourced session/publication grid and closures.",
        "market-calendar-build": "Generate a bounded versioned exchange schedule with the actual receipt clock.",
        "market-analysis": "Analyze one explicit native instrument series with session-aware coverage.",
        "market-plan": "Simulate a hypothetical native cash plan with declared fees and distributions.",
        "market-import": "Validate a versioned local bundle; --apply atomically imports it.",
        "market-fetch": "Inspect a source request; --apply explicitly fetches and persists a finite bundle.",
        "market-snapshot": "Inspect a named market snapshot; --apply explicitly creates an immutable export.",
        "market-fleet": "Read bounded native summaries from a verified mixed-class analytical snapshot.",
        "market-freeze": "Freeze a cross-asset prospective declaration without replacing an original.",
        "market-issue": "Preview a forecast request; --apply issues it at the actual clock.",
        "market-outcome": "Read a frozen instrument forecast and eligible later outcome without writes.",
        "market-futures-value": "Value a declared futures position under explicit contract/margin assumptions.",
        "market-futures-curve": "Construct a declared known-at continuous series; no executable fills.",
        "market-series": "Inspect bounded native intervals, vintages and provenance for one series.",
    }
    for name, description in descriptions.items():
        example = f"crypto-market-analysis {name}"
        if name not in {"market-capabilities", "market-registry", "market-snapshot"}:
            example += " --request request.json"
        parser = command(name, description, example)
        parser.add_argument("--request", type=Path,
                            required=name not in {"market-capabilities", "market-registry", "market-snapshot"},
                            help="Bounded versioned JSON request, or - for stdin")
        parser.add_argument("--format", choices=("json",), default="json")
        parser.add_argument("--output", help="Optional report file; frozen originals cannot be replaced")
        if name in WRITERS:
            parser.add_argument("--apply", action="store_true", help="Explicit finite local mutation")
        if name in {"market-snapshot", "market-fleet"}:
            parser.add_argument("--name", default="default", help="Safe snapshot name under the configured data directory")


def _run_read(args, settings, request, as_of):
    from .cross_asset import application

    routes = {
        "market-registry": application.market_registry,
        "market-calendar": application.market_calendar,
        "market-analysis": application.market_analysis,
        "market-plan": application.market_plan,
        "market-series": inspect_market_series,
    }
    if args.command in routes:
        return routes[args.command](settings.database, request, as_of=as_of)
    if args.command == "market-capabilities":
        _fields(request, ())
        from .cross_asset.providers import provider_catalog

        return {**application.market_capabilities(), "sources": provider_catalog()}
    if args.command == "market-fleet":
        return market_fleet(settings, args.name, request, as_of=as_of)
    if args.command == "market-outcome":
        from .cross_asset.prospective import evaluate_market_assessment

        return evaluate_market_assessment(settings.database, request, as_of=as_of)
    if args.command in {"market-futures-value", "market-futures-curve"}:
        from .cross_asset.contracts import build_continuous_series, value_futures_position

        if args.command == "market-futures-curve":
            return build_continuous_series(request, as_of=as_of)
        values = _fields(request, {"contract", "position", "native_currency"}, ("contract", "position", "native_currency"))
        return value_futures_position(values["contract"], values["position"], native_currency=values["native_currency"], as_of=as_of)
    raise ValueError("Unsupported cross-asset read command.")


def run_market_command(args, settings, request):
    from .cross_asset.application import validate_bundle
    from .cross_asset.storage import CrossAssetStore

    request = {} if request is None else request
    as_of = int(time.time()) if args.as_of is None else args.as_of
    if args.command == "market-calendar-build":
        from .cross_asset.sessions import generate_exchange_calendar

        if args.as_of is not None:
            raise ValueError("Calendar generation records the actual clock; omit --as-of.")
        return generate_exchange_calendar(**_fields(request, {"mic", "start_date", "end_date"},
                                                     ("mic", "start_date", "end_date")), known_at=as_of)
    if args.command == "market-freeze":
        from .cross_asset.prospective import freeze_market_protocol

        if args.as_of is not None:
            raise ValueError("Protocol freezing records the actual clock; omit --as-of.")
        if args.output and Path(args.output).exists():
            output = Path(args.output)
            if output.is_symlink() or output.stat().st_size > 2_000_000:
                raise ValueError("Cannot replace this frozen market protocol.")
            previous = json.loads(output.read_text(encoding="utf-8"))
            candidate = freeze_market_protocol(request, frozen_at=previous["frozen_at"])
            if candidate != previous:
                raise ValueError("A frozen market protocol cannot be replaced by a different declaration.")
            return previous
        return freeze_market_protocol(request, frozen_at=as_of)
    if args.command not in WRITERS:
        return _run_read(args, settings, request, as_of)
    if args.as_of is not None:
        raise ValueError("Market writes preserve the actual receipt/issuance clock; omit --as-of.")
    if args.command == "market-snapshot" and not args.apply:
        return {**market_snapshot_status(settings, args.name), "applied": False}
    if args.command == "market-import":
        bundle = validate_bundle(request)
        if not args.apply:
            return {"status": "validated", "applied": False, "network_requested": False,
                    "instruments": len(bundle["registry"]["instruments"]),
                    "series": len(bundle["series"]), "bars": sum(len(s["bars"]) for s in bundle["series"])}
    elif args.command == "market-fetch":
        _fields(request, {"registry", "calendars", "source_request"}, ("registry", "calendars", "source_request"))
        # Validate every shared identity before making a network request.
        descriptor = request["source_request"]["descriptor"]
        from .cross_asset.application import validate_capture_metadata

        validate_capture_metadata(request["registry"], request["calendars"], descriptor,
                                  start=request["source_request"]["start"],
                                  end=request["source_request"]["end"], as_of=as_of)
        if not args.apply:
            from .cross_asset.providers import provider_catalog

            return {"status": "preview", "applied": False, "network_requested": False,
                    "descriptor": descriptor, "sources": provider_catalog()}
    elif args.command == "market-issue" and not args.apply:
        return {"status": "preview", "applied": False, "network_requested": False,
                "notice": "Forecast issuance requires --apply and a matching frozen protocol at the actual clock."}

    from .dataset_lock import store_writer_lock

    with store_writer_lock(settings.database, create=args.command in {"market-import", "market-fetch"}):
        if args.command == "market-fetch":
            from .cross_asset.providers import fetch_series

            fetched = fetch_series(request["source_request"])
            if fetched.get("series") is None:
                return {**fetched, "applied": False}
            bundle = validate_bundle({"schema": "market-bundle.v1", "registry": request["registry"],
                                      "calendars": request["calendars"], "series": [fetched["series"]]})
        if args.command in {"market-import", "market-fetch"}:
            receipt = int(time.time())
            result = CrossAssetStore(settings.database, initialize=True).import_bundle(bundle, received_at=receipt)
            return {"status": "imported", "applied": True, "receipt": result,
                    **({"source_assessment": fetched["source_assessment"],
                        "http_accounting": fetched["http_accounting"]} if args.command == "market-fetch" else {})}
        if args.command == "market-issue":
            from .cross_asset.prospective import issue_market_assessment

            return issue_market_assessment(settings.database, request, issued_at=int(time.time()))
        from .cross_asset.snapshots import export_market_snapshot

        values = _fields(request, {"series_ids", "data_mode", "start", "end", "max_rows", "batch_rows"})
        manifest = export_market_snapshot(CrossAssetStore(settings.database), market_snapshot_path(settings, args.name), **values)
        return {"status": "exported", "applied": True, "manifest": manifest}
