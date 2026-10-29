"""Finite operating workflows exposed before any writable Engine is constructed."""

from __future__ import annotations

import json
from pathlib import Path

COMMANDS = frozenset({"http-accounting", "auto-repair-history", "analysis-dossier", "qualify-installation"})


def register_capacity_commands(command, parse_time) -> None:
    descriptions = {
        "http-accounting": ("Read bounded recorded HTTP work; unknown transport coverage remains unknown.",
                            "crypto-market-analysis http-accounting --data-dir data/live --source kraken"),
        "auto-repair-history": ("Plan recoverable named-dataset gaps; --apply explicitly runs a bounded repair cycle.",
                                "crypto-market-analysis auto-repair-history --mode import --data-dir data/research"),
        "analysis-dossier": ("Read context, stored analyses and calendar from one coherent SQLite snapshot.",
                             "crypto-market-analysis analysis-dossier --request examples/analysis-dossier.json"),
        "qualify-installation": ("Inspect four native markets locally; --refresh explicitly enables bounded public capture.",
                                 "crypto-market-analysis qualify-installation --request examples/installation-qualification.json"),
    }
    for name, (description, example) in descriptions.items():
        parser = command(name, description, example)
        parser.add_argument("--format", choices=("json", "markdown"), default="json")
        parser.add_argument("--output", help="Report file; omission writes stdout")
        if name == "http-accounting":
            parser.add_argument("--source", help="Explicit source; omission selects all recorded sources")
            parser.add_argument("--task", help="Explicit collection task, for example market")
            parser.add_argument("--start", type=parse_time, help="Earliest recorded UTC receipt")
            parser.add_argument("--end", type=parse_time, help="Latest recorded UTC receipt")
            parser.add_argument("--limit", type=int, default=1000, help="Maximum selected run records")
            parser.add_argument("--endpoint-limit", type=int, default=100, help="Maximum endpoint aggregates")
        else:
            parser.add_argument("--request", type=Path,
                                required=name == "analysis-dossier",
                                help="Bounded JSON request file, or - for stdin")
        if name == "auto-repair-history":
            parser.add_argument("--apply", action="store_true", help="Explicitly capture bounded repairs; default is inspection")
        if name == "qualify-installation":
            parser.add_argument("--refresh", action="store_true", help="Explicitly capture recent public data before qualification")


def run_capacity_command(args, settings, request: dict | None) -> dict:
    """Dispatch only explicit finite work; all inspection paths are noninitializing."""
    if args.command == "http-accounting":
        from .http_accounting import stored_http_accounting

        if args.end is not None and args.as_of is not None and args.end != args.as_of:
            raise ValueError("--end conflicts with --as-of; use one receipt cutoff.")
        return stored_http_accounting(settings.database, source=args.source, task=args.task, mode=args.mode,
                                      start=args.start, end=args.end if args.end is not None else args.as_of,
                                      limit=args.limit, endpoint_limit=args.endpoint_limit)
    request = dict(request or {})
    request.setdefault("data_mode", args.mode)
    if args.command == "analysis-dossier":
        from .analysis_dossier import build_analysis_dossier
        from .storage import Store

        return build_analysis_dossier(Store(settings.database, initialize=False), request, args.as_of)
    if args.command == "qualify-installation":
        from .installation_qualification import run_installation_qualification

        if args.refresh:
            request["refresh"] = True
        if args.as_of is not None:
            if request.get("as_of") is not None and request["as_of"] != args.as_of:
                raise ValueError("--as-of conflicts with the qualification request.")
            request["as_of"] = args.as_of
        return run_installation_qualification(settings, request)
    if args.command == "auto-repair-history":
        from .automatic_repair import run_auto_repairs

        mode = request.pop("data_mode")
        if args.as_of is not None:
            raise ValueError("Repair inspection and capture use the actual clock; omit --as-of.")
        if not args.apply:
            return run_auto_repairs(settings.database, request, mode=mode)
        from .history_dataset import KrakenRESTCapture

        fetch = KrakenRESTCapture()
        try:
            return run_auto_repairs(settings.database, request, apply=True, fetch=fetch, mode=mode)
        finally:
            session = getattr(fetch.client, "session", None)
            if session is not None:
                session.close()
    raise ValueError("Unsupported operating workflow command.")


def render_capacity_report(result: dict, command: str) -> str:
    """Keep the complete bounded machine report inspectable in a text document."""
    title = {"http-accounting": "Observed HTTP work", "auto-repair-history": "Bounded history repair",
             "analysis-dossier": "Coherent analysis dossier", "qualify-installation": "Local installation qualification"}[command]
    return f"# {title}\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)}\n```\n"
