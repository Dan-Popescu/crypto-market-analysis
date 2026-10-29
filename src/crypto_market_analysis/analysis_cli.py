"""Finite analytical workflows; database reads never initialize configured stores."""

from __future__ import annotations

from pathlib import Path

COMMANDS = frozenset({"analysis-changes", "analysis-readiness", "backfill-history", "plan-robustness"})


def register_analysis_commands(command) -> None:
    descriptions = {
        "analysis-changes": ("Explain native market, revision, availability and collection changes between two cutoffs.",
                             "crypto-market-analysis analysis-changes --request examples/analysis-changes.json"),
        "analysis-readiness": ("Diagnose supported analytical claims and missing inputs without collecting data.",
                               "crypto-market-analysis analysis-readiness --request examples/analysis-readiness.json"),
        "backfill-history": ("Inspect resumable native history; --apply explicitly captures one bounded import cycle.",
                             "crypto-market-analysis backfill-history --mode import --request examples/backfill-history.json"),
        "plan-robustness": ("Compare fixed plan origins, declared costs and missing-hour stresses without optimization.",
                            "crypto-market-analysis plan-robustness --request examples/plan-robustness.json"),
    }
    for name, (description, example) in descriptions.items():
        parser = command(name, description, example)
        parser.add_argument("--request", type=Path, required=name != "backfill-history",
                            help="Bounded JSON request file, or - for stdin")
        parser.add_argument("--format", choices=("json", "markdown"), default="json")
        parser.add_argument("--output", help="Report file outside retained evidence; omission writes stdout")
        if name == "backfill-history":
            parser.add_argument("--apply", action="store_true", help="Explicitly run one finite public capture cycle")
            parser.add_argument("--status", action="store_true", help="Inspect bounded current jobs without a request or recovery")
            parser.add_argument("--limit", type=int, default=20, help="Maximum current jobs in --status output")


def run_analysis_command(args, settings, request):
    if args.command == "backfill-history":
        from .history_backfill import (
            history_backfill_status,
            normalize_backfill_request,
            run_history_backfill,
        )

        if args.as_of is not None:
            raise ValueError("History planning and capture use the actual clock; omit --as-of.")
        if args.status:
            if args.apply or request is not None:
                raise ValueError("--status is read-only and cannot be combined with --apply or --request.")
            return history_backfill_status(settings.database, mode=args.mode, limit=args.limit)
        if request is None:
            raise ValueError("Use backfill-history --mode import --request examples/backfill-history.json, or --status.")
        if args.mode != "import":
            raise ValueError("Retrospective backfill requires --mode import.")
        request = normalize_backfill_request(request)
        if not args.apply:
            return run_history_backfill(settings.database, request)
        from .http_limits import HTTPPolicy, http_budget_scope

        policy = HTTPPolicy(max_requests=min(request["http_max_requests"], settings.http_max_requests),
                            deadline_seconds=min(request["deadline_seconds"], settings.http_deadline_seconds))
        with http_budget_scope(policy):
            return run_history_backfill(settings.database, request, apply=True)
    from .storage import Store

    store = Store(settings.database, initialize=False)
    if args.command == "analysis-changes":
        from .analysis_changes import build_analysis_changes

        return build_analysis_changes(store, request, args.as_of)
    if args.command == "analysis-readiness":
        from .analysis_readiness import build_analysis_readiness

        return build_analysis_readiness(store, request, args.as_of)
    if args.command == "plan-robustness":
        from .robustness import build_plan_robustness

        return build_plan_robustness(store, request, args.as_of)
    raise ValueError("Unsupported analytical workflow command.")


def render_analysis_report(result, command):
    if command == "analysis-changes":
        from .analysis_changes import render_analysis_changes_markdown

        return render_analysis_changes_markdown(result)
    if command == "analysis-readiness":
        from .analysis_readiness import render_analysis_readiness_markdown

        return render_analysis_readiness_markdown(result)
    if command == "plan-robustness":
        from .robustness import render_plan_robustness_markdown

        return render_plan_robustness_markdown(result)
    lines = ["# Resumable native history", "", f"Status: {result['status']}; applied: {result.get('applied', False)}", ""]
    if "jobs" in result:
        lines.extend([f"Jobs included: {len(result['jobs'])}; omitted: {result.get('omitted_jobs', 0)}.", "",
                      "| Job | Source | State |", "| --- | --- | --- |"])
        for job in result["jobs"]:
            lines.append("| " + " | ".join(_cell(job.get(key)) for key in ("job_id", "source", "status")) + " |")
    else:
        request = result.get("request", {})
        lines.extend([f"Source: {_cell(request.get('source'))}; mode: import.", "",
                      "| Asset | Native quote | Covered hours | Missing closed hours | Unclosed hours |",
                      "| --- | --- | --- | --- | --- |"])
        for row in result.get("markets", []):
            lines.append("| " + " | ".join(_cell(row.get(key)) for key in
                         ("asset", "quote", "covered_hours", "missing_hours", "unclosed_hours")) + " |")
    lines.extend(["", "Historical imports retain actual receipt times; original historical availability remains unknown.",
                  "Missing hours are not skipped. JSON retains exact checkpoints, budgets and qualification limits.", ""])
    return "\n".join(lines)


def _cell(value):
    return "unknown" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def analysis_exit_code(command, result, *, apply=False):
    if command == "backfill-history" and apply:
        cycle = result.get("cycle", {})
        failed = any(row.get("error") for row in cycle.get("attempts", []))
        exceeded = (cycle.get("http_budget") or {}).get("exceeded")
        return 0 if (result.get("applied") and result.get("status") in ("complete", "incomplete")
                     and not failed and not exceeded) else 1
    return 0
