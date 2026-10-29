"""Finite operating workflows exposed before any writable Engine is constructed."""

from __future__ import annotations

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
    """Render useful summaries; the JSON format retains full bounded evidence."""
    if command == "qualify-installation":
        from .installation_qualification import render_installation_qualification

        return render_installation_qualification(result)
    if command == "analysis-dossier":
        return _render_dossier(result)
    if command == "http-accounting":
        summary = result["summary"]
        lines = ["# Observed HTTP work", "", f"Status: {result['status']}; coverage: {summary['coverage']}", "",
                 f"Selected retained runs: {_cell(result['included_runs'])}; omitted: {_cell(result['omitted_runs'])}.", "",
                 "| Source | Task | Coverage | Attempts | Responses | Errors | Decoded body bytes | Elapsed ms |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for row in result["sources"]:
            totals = row.get("totals") or {}
            cells = [row["source"], row["task"], row["coverage"], *[totals.get(key) for key in
                     ("requests", "responses", "errors", "decoded_body_bytes", "elapsed_ms")]]
            lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
        lines.extend(["", "Body bytes are decoded application data. Wire, request and TLS byte totals remain unknown.",
                      "Counts cover selected retained runs; unknown instrumentation does not mean zero work.",
                      "Use JSON for exact endpoint aggregates and selection limits.", ""])
        return "\n".join(lines)
    lines = ["# Bounded history repair", "", f"Status: {result['status']}; applied: {result.get('applied', False)}", "",
             f"Planned requests: {result.get('requests_planned', 0)}; planned hours: {result.get('hours_planned', 0)}.", "",
             "| Asset | Native quote | Missing hours | API recoverable | Requires archives | Not yet closed |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in result["markets"]:
        lines.append("| " + " | ".join(_cell(row.get(key)) for key in
                     ("asset", "quote", "remaining_hours", "recoverable_hours", "requires_archives_hours", "not_closed_hours")) + " |")
    if result.get("status") == "busy":
        lines.extend(["", "Another process holds the dataset writer lock; this invocation performed no capture or recovery."])
    if result.get("scheduling"):
        market = result["scheduling"]["next_market"]
        lines.extend(["", f"Next eligible turn starts at {_cell(market[0])}/{_cell(market[1])}; unavailable markets are skipped."])
    if result.get("http_accounting"):
        measurement = result["http_accounting"]
        totals = measurement.get("totals") or {}
        lines.extend(["", f"HTTP coverage: {_cell(measurement['coverage'])}; "
                      f"observed attempts: {_cell(totals.get('requests'))}; "
                      f"responses: {_cell(totals.get('responses'))}.",
                      "Checkpointed capture attempts and observed HTTP requests are separate counts."])
    if result.get("outcomes"):
        lines.extend(["", "| Task | Asset | Native quote | Status | Remaining hours | Retry at UTC |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for row in result["outcomes"]:
            lines.append("| " + " | ".join(_cell(row.get(key)) for key in
                         ("id", "asset", "quote", "status", "remaining_hours", "retry_at")) + " |")
    lines.extend(["", "Only closed hours inside the documented recent API retention can be repaired here.",
                  "Older missing hours need archives. JSON retains exact task ranges and checkpoints.", ""])
    return "\n".join(lines)


def _cell(value) -> str:
    return "unknown" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _render_dossier(result) -> str:
    from .reporting import render_accumulation_report

    lines = ["# Coherent analysis dossier", "", f"UTC cutoff: {result['as_of']}; mode: {result['data_mode']}",
             f"Snapshot: {result['snapshot_id']}", ""]
    section_kinds = {"venue_disagreement": "stored-venue-disagreement", "venue_costs": "stored-venue-costs",
                     "derivatives": "stored-derivatives", "quotes": "stored-quotes"}
    for market in result["markets"]:
        native = market["selection"]
        lines.extend([f"## {native['asset']}/{native['quote']} — {native['source']}", "",
                      render_accumulation_report(market["market_context"], "context")])
        for section, kind in section_kinds.items():
            if market[section].get("status") == "not_requested":
                continue
            lines.append(render_accumulation_report(market[section], kind))
    lines.extend(["## Known-at upcoming calendar", "", f"Status: {result['calendar']['status']}", "",
                  "| Source | Event time/date | Title | Status |", "| --- | --- | --- | --- |"])
    for row in result["calendar"]["rows"]:
        lines.append("| " + " | ".join(_cell(value) for value in
                     (row.get("source"), row.get("event_time", row.get("event_date")),
                      row.get("title", row.get("name")), row.get("status"))) + " |")
    lines.extend(["", *result["limitations"], "", "Use JSON for complete consumed evidence and hashes.", ""])
    return "\n".join(lines)


def capacity_exit_code(command: str, result: dict, *, apply: bool = False) -> int:
    """Distinguish a valid diagnostic report from a passed acceptance check."""
    if command == "qualify-installation":
        return 0 if result["status"] == "pass" else 1
    if command == "auto-repair-history" and apply:
        failed = any(row["status"] in ("error", "unsupported", "unknown_gap") for row in result.get("outcomes", []))
        return 1 if not result.get("applied") or failed else 0
    return 1 if result.get("status") == "error" else 0
