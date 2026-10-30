"""Non-interactive CLI; stdout is JSON or the requested report, logs use stderr."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .asset_universe import validate_asset
from .config import Settings, load_settings
from .engine import Engine, render_brief
from .models import Candle
from .reporting import (
    bounded_as_of,
    bounded_integers,
    bounded_object,
    bounded_origins,
    bounded_thresholds,
    render_accumulation_report,
)
from .venues import VENUES

EXAMPLE = """# Crypto Market Analysis — no paid collection APIs
data_dir = "data"
assets = ["BTC", "ETH"]
venues = ["binance", "coinbase", "kraken", "okx", "bybit", "bitstamp", "bitfinex", "kucoin", "gate", "cryptocom", "gemini", "bitget", "mexc", "htx", "bitvavo"]
quote_currencies = ["EUR", "USD", "USDT", "USDC"]
purchase_jurisdiction = "France"
purchase_candidates = ["kraken", "bitvavo", "coinbase"]
liquidity_venues = ["kraken", "bitvavo", "coinbase"]
history_days = 30
max_pages = 20
http_max_requests = 10000
collection_batch_size = 10
collection_work_limit = 256
# liquidity_assets = ["BTC", "ETH"]
http_deadline_seconds = 300.0
workers = 3
options = true
context = true
liquidity_interval = 300
market_interval = 3600
options_interval = 21600
context_interval = 86400
liquidity_retention_days = 7
"""


def parse_time(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("The date requires a timezone, for example 2026-10-28T12:00:00Z.") from None
        return int(dt.timestamp())


def write_json(value: object, output: str | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _json_file(path: Path) -> dict:
    if str(path) == "-":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        payload = stream.read(2_000_001)
        payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(payload) > 2_000_000:
            raise ValueError("Configuration exceeds the 2 MB limit.")
        content = payload.decode("utf-8")
    else:
        if path.stat().st_size > 2_000_000:
            raise ValueError("Configuration exceeds the 2 MB limit.")
        content = path.read_text(encoding="utf-8")
    try:
        value = json.loads(content)
    except RecursionError as exc:
        raise ValueError("Configuration exceeds the JSON nesting limit.") from exc
    return bounded_object(value)


def _csv_numbers(value: str | None, cast):
    return None if value is None else [cast(item) for item in value.split(",")]


def _write_result(result: dict, args) -> None:
    if args.format == "json":
        write_json(result, args.output)
    else:
        text = render_accumulation_report(result, args.command)
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        else:
            print(text, end="")


def _stored_request(args) -> dict:
    request = dict(_json_file(args.request)) if args.request else {}
    for key in ("asset", "quote", "source", "sources"):
        value = getattr(args, key, None)
        if value is not None:
            value = value.split(",") if key == "sources" else value
            if key in request and request[key] != value:
                raise ValueError(f"--{key} conflicts with the JSON request.")
            request[key] = value
    request.setdefault("data_mode", args.mode)
    if args.command.startswith("stored-venue-"):
        analysis = "purchase_costs" if args.command == "stored-venue-costs" else "disagreement"
        if "analysis" in request and request["analysis"] != analysis:
            raise ValueError("The JSON analysis conflicts with the selected command.")
        request["analysis"] = analysis
    return bounded_object(request, "stored analysis request")


def _protect_report_output(args, settings) -> None:
    """Do not allow report output to overwrite the SQLite evidence store."""
    if not args.output:
        return
    output, database = Path(args.output).resolve(), settings.database.resolve()
    protected = {database, Path(str(database) + "-wal"), Path(str(database) + "-shm"),
                 settings.data_dir.resolve() / "dataset.json"}
    request_path = getattr(args, "request", None)
    dataset = settings.data_dir.resolve() / "dataset.json"
    recovery_bundle = any((parent / "manifest.json").is_file() for parent in output.parents)
    if (output in protected or (request_path and output == request_path.resolve())
            or (dataset.is_file() and output.is_relative_to(settings.data_dir.resolve()))
            or (output.is_file() and output.stat().st_nlink > 1)
            or recovery_bundle
            or (output.exists() and output.suffix.lower() in (".payload", ".sqlite", ".sqlite3", ".db", ".parquet"))):
        raise ValueError("Report output must not overwrite the database, its sidecars, dataset identity, request or a hard-linked file.")


def _history_operation(args) -> dict:
    from .dataset_lock import DatasetBusy, dataset_writer_lock
    from .history_dataset import DatasetConfig

    if args.as_of is not None:
        raise ValueError("Dataset operations preserve actual capture times; --as-of is for analysis commands.")
    raw = _json_file(args.dataset_config)
    directory = Path(raw["directory"]).expanduser()
    raw["directory"] = str(directory if directory.is_absolute() else args.dataset_config.resolve().parent / directory)
    for key in ("target_start", "target_end"):
        if isinstance(raw.get(key), str):
            raw[key] = parse_time(raw[key])
    config = DatasetConfig(**raw)
    _protect_report_output(args, Settings(data_dir=Path(config.directory)))
    if args.output:
        output = Path(args.output).resolve()
        inputs = [args.dataset_config, args.parts_manifest]
        if (output.is_relative_to(Path(config.directory).resolve())
                or any(output == path.resolve() for path in inputs if path is not None)):
            raise ValueError("Report output must not overwrite dataset evidence or its configuration and archive inputs.")
    if args.action != "init" and not (Path(config.directory) / "dataset.json").is_file():
        raise ValueError("Initialize this explicit research dataset first with history-dataset init.")
    archive = _history_archive_inputs(args) if args.action == "stage" else None
    database = Path(config.directory) / "observer.sqlite"
    # Construction can replay pending journals, including for legacy status.
    # New dataset creation still requires an isolated empty directory.
    lock = dataset_writer_lock(database) if (database.parent / "dataset.json").is_file() else nullcontext()
    try:
        with lock:
            return _run_history_operation(args, config, archive)
    except DatasetBusy:
        return {"status": "busy", "reason": "dataset_writer_busy", "applied": False,
                "dataset": config.name, "data_mode": "import", "action": args.action}


def _history_archive_inputs(args):
    from .history_dataset import ArchivePart

    if args.parts_manifest is None or not args.member or args.captured_at is None:
        raise ValueError("Archive staging requires --parts-manifest, --member and --captured-at.")
    manifest = _json_file(args.parts_manifest)
    if not isinstance(manifest.get("parts"), list):
        raise ValueError("A parts manifest requires an explicitly ordered parts array.")
    parts = []
    for item in manifest["parts"]:
        part = dict(item)
        path = Path(part["path"]).expanduser()
        part["path"] = str(path if path.is_absolute() else args.parts_manifest.resolve().parent / path)
        if args.output and Path(args.output).resolve() == Path(part["path"]).resolve():
            raise ValueError("Report output must not overwrite an archive input.")
        parts.append(ArchivePart(**part))
    return parts, manifest.get("build_metadata")


def _run_history_operation(args, config, archive) -> dict:
    from .history_dataset import HistoryDataset, KrakenRESTCapture
    from .http_limits import HTTPPolicy, http_budget_scope

    dataset = HistoryDataset(config)
    if args.action in ("init", "status"):
        return dataset.manifest()
    if args.action == "stage":
        parts, build_metadata = archive
        return dataset.stage_archive(parts, member=args.member, asset=args.asset, quote=args.quote,
                                     captured_at=args.captured_at, build_metadata=build_metadata)
    if args.action == "plan-repair":
        if args.start is None or args.end is None:
            raise ValueError("A bounded repair plan requires --start and --end.")
        return dataset.plan_repair(args.asset, args.quote, args.start, args.end)
    if args.action == "refresh" and args.since is None:
        raise ValueError("One-shot refresh requires an explicit --since timestamp.")
    fetch = KrakenRESTCapture()
    try:
        config_path = args.config or (Path("observer.toml") if Path("observer.toml").exists() else None)
        policy = HTTPPolicy.from_settings(load_settings(config_path))
        with http_budget_scope(policy):
            if args.action == "refresh":
                return dataset.append_rest(args.asset, args.quote, fetch, since=args.since, limit=args.limit)
            return dataset.repair(args.asset, args.quote, fetch, request_budget=args.request_budget)
    except Exception as exc:
        raise ValueError(f"Dataset public capture failed: {type(exc).__name__}; transport details withheld.") from exc
    finally:
        session = getattr(fetch.client, "session", None)
        if session is not None and callable(getattr(session, "close", None)):
            session.close()


def _common(parser: argparse.ArgumentParser, sub: bool = False) -> None:
    default = argparse.SUPPRESS if sub else None
    parser.add_argument("--config", type=Path, default=default, help="TOML configuration")
    parser.add_argument("--data-dir", type=Path, default=default, help="Data directory")
    parser.add_argument(
        "--mode", choices=("live", "demo", "import"), default=argparse.SUPPRESS if sub else "live"
    )
    parser.add_argument(
        "--as-of", type=parse_time, default=default, help="As-of time in ISO UTC format or epoch seconds"
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="crypto-market-analysis",
        description="Multi-asset analysis using bounded free public data.",
        epilog="Examples: crypto-market-analysis demo ; crypto-market-analysis brief --mode demo ; crypto-market-analysis collect --venues kraken,coinbase",
    )
    root.add_argument("--version", action="version", version=__version__)
    _common(root)
    subs = root.add_subparsers(dest="command", required=True)

    def command(name: str, description: str, example: str):
        p = subs.add_parser(name, help=description, description=description, epilog=f"Example: {example}")
        _common(p, True)
        return p

    from .capacity_cli import register_capacity_commands

    register_capacity_commands(command, parse_time)
    from .analysis_cli import register_analysis_commands

    register_analysis_commands(command)
    from .scalability_cli import register_scalability_commands

    register_scalability_commands(command)

    init = command(
        "init", "Create an editable configuration.", "crypto-market-analysis init --file observer.toml"
    )
    init.add_argument("--file", type=Path, default=Path("observer.toml"))
    command("venues", "Venue catalogue and documented regulatory status.", "crypto-market-analysis venues")
    collect = command(
        "collect",
        "Collect public endpoints once.",
        "crypto-market-analysis collect --venues kraken,coinbase --days 7",
    )
    collect.add_argument("--venues", help="Comma-separated identifiers")
    collect.add_argument("--quotes", help="Explicit native quotes, for example EUR,USD; no conversion")
    collect.add_argument(
        "--tasks", default="market,liquidity,options,context", help="market,liquidity,options,context"
    )
    collect.add_argument("--days", type=int, help="Requested history; availability depends on the venue")
    collect.add_argument("--max-pages", type=int, help="Pagination bounded between 1 and 100")
    command(
        "run",
        "Continuous collector using TOML schedules; stop with Ctrl-C.",
        "crypto-market-analysis run --config observer.toml",
    )
    command("status", "Source status, freshness and stored data counts.", "crypto-market-analysis status")
    brief = command(
        "brief",
        "English report or structured dossier for AI.",
        "crypto-market-analysis brief --asset BTC --format json",
    )
    brief.add_argument("--format", choices=("markdown", "json"), default="markdown")
    brief.add_argument("--cadence", choices=("daily", "weekly"), default="daily")
    brief.add_argument("--output")
    forecast = command(
        "forecast",
        "Explicitly experimental historical estimates.",
        "crypto-market-analysis forecast --asset BTC --horizons 7,21,60",
    )
    forecast.add_argument("--horizons", default="1,7,21,30,60")
    forecast.add_argument("--threshold", type=float, default=0.1, help="Relative barrier, for example 0.10")
    forecast.add_argument("--output")
    evaluation = command(
        "evaluate",
        "Chronological validation against a historical base rate.",
        "crypto-market-analysis evaluate --asset BTC --horizon 7 --output evaluation.json",
    )
    evaluation.add_argument("--horizon", type=int, default=7)
    evaluation.add_argument("--threshold", type=float, default=0.1)
    evaluation.add_argument("--output")
    evaluation.add_argument(
        "--validation",
        choices=("known_at", "retrospective"),
        default="known_at",
        help="known_at uses data available at each origin; retrospective uses the current historical dataset",
    )
    chart = command(
        "chart",
        "PNG or SVG chart with provenance.",
        "crypto-market-analysis chart --mode demo --kind price --output output/btc.png",
    )
    chart.add_argument(
        "--kind",
        choices=(
            "price",
            "weekly",
            "liquidity",
            "leverage",
            "options",
            "calibration",
            "context",
            "windows",
            "plans",
        ),
        default="weekly",
    )
    chart.add_argument("--output", type=Path, required=True)
    chart.add_argument("--horizon", type=int, default=7)
    chart.add_argument("--plan-config", type=Path, help="Required JSON configuration for a plans chart")
    chart.add_argument(
        "--validation",
        choices=("known_at", "retrospective"),
        default="known_at",
        help="Evaluation mode for the calibration chart",
    )
    export = command(
        "export",
        "Export the latest versions as compressed Parquet.",
        "crypto-market-analysis export --output output/parquet",
    )
    export.add_argument("--output", type=Path, required=True)
    maintenance = command(
        "maintain",
        "Compact older order book detail into hourly medians.",
        "crypto-market-analysis maintain --keep-days 7",
    )
    maintenance.add_argument("--apply", action="store_true", help="Write checked archives and prune eligible hot evidence; default inspection only")
    maintenance.add_argument(
        "--keep-days", type=int, help="Days of detail to retain; defaults to TOML configuration"
    )
    storage_status = command("storage-policy", "Inspect storage families, physical bytes and conservative budgets.",
                             "crypto-market-analysis storage-policy")
    storage_status.add_argument("--output")
    archive_verify = command("verify-archive", "Verify one lossless retention archive offline.",
                             "crypto-market-analysis verify-archive --archive file.json")
    archive_verify.add_argument("--archive", type=Path, required=True)
    archive_restore = command("restore-archive", "Restore checked evidence; default verifies without writing.",
                              "crypto-market-analysis restore-archive --archive file.json --apply")
    archive_restore.add_argument("--archive", type=Path, required=True)
    archive_restore.add_argument("--apply", action="store_true")
    events = command(
        "import-events",
        "Import a sourced JSON calendar without claiming complete coverage.",
        "crypto-market-analysis import-events --file events.json",
    )
    events.add_argument("--file", type=Path, required=True)
    candles = command(
        "import-candles",
        "Import hourly CSV candles with explicit provenance.",
        "crypto-market-analysis import-candles --file candles.csv --source my-feed",
    )
    candles.add_argument("--file", type=Path, required=True)
    candles.add_argument("--source", required=True)
    candles.add_argument("--asset", type=validate_asset, default="BTC")
    candles.add_argument("--quote", default="USD")
    metrics = command(
        "import-metrics",
        "Import sourced CSV metrics (for example ETF flows) in import mode.",
        "crypto-market-analysis import-metrics --file flows.csv",
    )
    metrics.add_argument("--file", type=Path, required=True)
    demo = command(
        "demo", "Load SYNTHETIC data in a separate mode.", "crypto-market-analysis demo --days 1800"
    )
    demo.add_argument("--days", type=int, default=1800)
    demo.add_argument("--quotes", default="USD", help="Independent synthetic EUR,USD scenarios")
    serve = command(
        "serve",
        "MCP server using STDIO or Streamable HTTP.",
        "crypto-market-analysis serve --transport stdio",
    )
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    context = command(
        "context",
        "Descriptive native-quote context through two years.",
        "crypto-market-analysis context --asset BTC --lookbacks 30,365,730",
    )
    context.add_argument("--lookbacks", help="At most 16 unique day windows, 1–3650")
    windows = command(
        "windows",
        "Experimental purchase windows with reference frequencies.",
        "crypto-market-analysis windows --horizons 1,3,7,14",
    )
    windows.add_argument("--horizons", help="At most 16 unique horizons, 1–365 days")
    windows.add_argument("--thresholds", help="At most 12 unique decline fractions, for example 0.02,0.05")
    accumulation = command("accumulation-brief", "Review one pinned next purchase tranche from one snapshot.",
                           "crypto-market-analysis accumulation-brief --asset BTC --source kraken --quote EUR")
    accumulation.add_argument("--plan-config", type=Path)
    plan_init = command("plan-init", "Construct a local plan from UTC dates and explicit recurrence.",
                        "crypto-market-analysis plan-init --request request.json --output data/personal-plan.json")
    plan_init.add_argument("--request", type=Path, required=True)
    plan_init.add_argument("--output")
    preview = command("plan-preview", "Preview dated funding constraints without market prices.",
                      "crypto-market-analysis plan-preview --plan-config plan.json")
    preview.add_argument("--plan-config", type=Path, required=True)
    sensitivity = command("cost-sensitivity", "Compare at most three declared cost assumptions on frozen policies.",
                          "crypto-market-analysis cost-sensitivity --plan-config plan.json --cost-cases costs.json")
    sensitivity.add_argument("--cost-cases", type=Path, required=True, help='Object containing a "cases" array')
    journal_summary = command("journal-summary", "Read bounded journal cohorts and pending or missing outcomes.",
                              "crypto-market-analysis journal-summary --limit 20")
    journal_summary.add_argument("--limit", type=int, default=20, help="Most recent issuances, 1–100")
    scorecard = command("journal-scorecard", "Read descriptive outcomes and independently selected journal scenarios.",
                        "crypto-market-analysis journal-scorecard --limit 20")
    scorecard.add_argument("--limit", type=int, default=20, help="Most recent issuances, 1–100")
    budget = command("budget-preview", "Preview caller-declared plans sharing one price-free cash envelope.",
                     "crypto-market-analysis budget-preview --request budget.json")
    purchase_costs = command("purchase-costs", "Calculate fee-inclusive purchases from explicit native-quote ask ladders.",
                             "crypto-market-analysis purchase-costs --request costs.json")
    disagreement = command("venue-disagreement", "Describe contemporaneous native-quote venue dispersion.",
                           "crypto-market-analysis venue-disagreement --request venues.json")
    quote_context = command("quote-context", "Describe explicit indicative EUR/USD and stablecoin basis observations.",
                            "crypto-market-analysis quote-context --request quotes.json")
    derivatives = command("derivatives-context", "Describe native derivatives changes over fixed 4-hour and 24-hour endpoints.",
                          "crypto-market-analysis derivatives-context --request derivatives.json")
    for p in (budget, purchase_costs, disagreement, quote_context, derivatives):
        p.add_argument("--request", type=Path, required=True, help="Explicit bounded JSON object; no collection")
    capture_books = command("capture-books", "Explicit one-shot public ask/bid snapshot capture into SQLite; live clock only.",
                            "crypto-market-analysis capture-books --sources kraken --assets BTC,ETH --quotes EUR,USD")
    capture_books.add_argument("--sources", required=True, help="Comma-separated venue IDs; no purchase eligibility implied")
    capture_books.add_argument("--assets", required=True, help="Explicit comma-separated BTC,ETH subset")
    capture_books.add_argument("--quotes", required=True, help="Explicit native EUR,USD,USDT,USDC subset; no conversion")
    official = command("collect-official-context", "Explicit bounded capture of official FX, calendars and optional stablecoin tickers.",
                       "crypto-market-analysis collect-official-context --include fx,calendar --max-requests 12")
    official.add_argument("--include", default="fx,calendar", help="Distinct fx,calendar,stablecoins families; defaults to fx,calendar")
    official.add_argument("--venues", default="kraken", help="At most four public ticker venues when stablecoins is selected")
    official.add_argument("--max-requests", type=int, default=12, help="Logical adapter operation ceiling, 1–32; calendars reserve three operations; not a wire HTTP limit")
    stored = []
    for name, description in (
        ("stored-derivatives", "Read fixed native derivatives endpoints from SQLite; no network."),
        ("stored-quotes", "Read selected native quote references from SQLite; no implicit conversion or network."),
        ("stored-venue-costs", "Read retained ask ladders with caller-declared fees; unknown fees remain unknown."),
        ("stored-venue-disagreement", "Read contemporaneous retained native-quote venue snapshots; no network."),
    ):
        p = command(name, description, f"crypto-market-analysis {name} --request request.json --as-of 1793145600")
        p.add_argument("--request", type=Path, help="Bounded selector/policy JSON; JSON data_mode takes precedence over --mode. Costs require budget; assumptions_by_source supplies fees, otherwise unknown")
        p.add_argument("--asset", type=validate_asset, help="Native asset (required for derivatives and venue views)")
        p.add_argument("--quote", choices=("EUR", "USD", "USDT", "USDC"), help="Explicit native quote; no conversion")
        if name == "stored-derivatives":
            p.add_argument("--source", help="Required explicit derivative source, for example binanceusdm")
        else:
            p.add_argument("--sources", help="Required distinct comma-separated quote/venue sources, for example ecb,kraken")
        stored.append(p)
    qualification = command("qualify-sources", "Read-only source/history/resource qualification; no network or issuance.",
                            "crypto-market-analysis qualify-sources --request qualification.json")
    workflow = command("local-workflow", "Run a bounded local report; request refresh and assessment issuance are explicit opt-ins.",
                       "crypto-market-analysis local-workflow --request workflow.json --format markdown")
    qualification.add_argument("--request", type=Path, required=True,
                               help="JSON: sources, markets (native BTC/EUR etc), data_mode, required_history_days and resource bounds")
    workflow.add_argument("--request", type=Path, required=True,
                          help="JSON: refresh defaults false; live refresh forbids as_of; issue_assessments requires idempotency_key")
    for p in (capture_books, official, *stored, qualification, workflow):
        p.add_argument("--format", choices=("json", "markdown"), default="json")
        p.add_argument("--output", help="Report path; omission writes stdout")
    backup = command("backup", "Create and verify an explicit SQLite recovery bundle.",
                     "crypto-market-analysis backup --database data/observer.sqlite --destination recovery")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--quiescent-dataset", action="store_true",
                        help="Explicitly declare the named research dataset writer is stopped before backup")
    verify = command("verify-backup", "Read and verify an existing recovery bundle.",
                     "crypto-market-analysis verify-backup --bundle recovery")
    verify.add_argument("--bundle", type=Path, required=True)
    restore = command("restore-backup", "Restore a verified bundle to an absent or empty explicit directory.",
                      "crypto-market-analysis restore-backup --bundle recovery --destination restored-data")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    for p in (backup, verify, restore):
        p.add_argument("--output")
        p.add_argument("--format", choices=("json",), default="json")
    dataset = command("history-dataset", "Manage one explicit import-only research dataset; network is opt-in.",
                      "crypto-market-analysis history-dataset init --dataset-config dataset.json")
    dataset.add_argument("action", choices=("init", "status", "stage", "refresh", "plan-repair", "repair"))
    dataset.add_argument("--dataset-config", type=Path, required=True)
    dataset.add_argument("--asset", type=validate_asset, default="BTC")
    dataset.add_argument("--quote", choices=("EUR", "USD"), default="EUR")
    dataset.add_argument("--parts-manifest", type=Path)
    dataset.add_argument("--member")
    dataset.add_argument("--captured-at", type=parse_time)
    dataset.add_argument("--since", type=parse_time)
    dataset.add_argument("--start", type=parse_time)
    dataset.add_argument("--end", type=parse_time)
    dataset.add_argument("--limit", type=int, default=720)
    dataset.add_argument("--request-budget", type=int, default=1)
    dataset.add_argument("--output")
    comparison = command(
        "compare-plans",
        "Compare hypothetical regular, split and deferred purchases.",
        "crypto-market-analysis compare-plans --plan-config plan.json",
    )
    plan_evaluation = command(
        "evaluate-plans",
        "Paired chronological purchase-plan evaluation.",
        "crypto-market-analysis evaluate-plans --plan-config plan.json --validation known_at",
    )
    for p in (comparison, plan_evaluation, sensitivity):
        p.add_argument("--plan-config", type=Path, required=True, help="Finite JSON object, at most 2 MB")
        p.add_argument(
            "--validation",
            choices=("known_at", "retrospective"),
            default="known_at" if p is plan_evaluation else "retrospective",
        )
    plan_evaluation.add_argument("--origins", help="At most 24 comma-separated UTC epochs or ISO dates")
    plan_evaluation.add_argument("--held-out-start", type=parse_time)
    plan_evaluation.add_argument("--policy-frozen-at", type=parse_time)
    issue = command(
        "issue",
        "Explicitly save an immutable local assessment.",
        "crypto-market-analysis issue --idempotency-key local-001 --asset BTC",
    )
    issue.add_argument("--idempotency-key", required=True)
    issue.add_argument("--plan-config", type=Path)
    issue.add_argument("--validation-protocol", type=Path, help="Frozen prospective protocol to bind before immutable issuance")
    assessment = command(
        "assessment",
        "Read an issued local assessment and appended outcomes.",
        "crypto-market-analysis assessment --id ASSESSMENT_ID",
    )
    assessment.add_argument("--id", required=True)
    replay = command("replay-assessment", "Verify original calculations from frozen issued evidence without appending outcomes.",
                     "crypto-market-analysis replay-assessment --id ASSESSMENT_ID")
    replay.add_argument("--id", required=True)
    observe = command(
        "observe",
        "Explicitly append observed outcomes to a local assessment.",
        "crypto-market-analysis observe --id ASSESSMENT_ID",
    )
    observe.add_argument("--id", required=True)
    vigilance = command(
        "vigilance",
        "Optional bounded 4/8-hour observational vigilance.",
        "crypto-market-analysis vigilance --asset BTC --horizon-hours 4",
    )
    vigilance.add_argument("--horizon-hours", type=int, choices=(4, 8), default=4)
    vigilance.add_argument("--policy", type=Path, help="Optional finite JSON policy object, at most 2 MB")
    vigilance.add_argument("--state-key", help="Explicitly persist a local transition; omission is read-only")
    command(
        "sources",
        "Source access, reuse rights and purchase-eligibility catalogue.",
        "crypto-market-analysis sources",
    )
    history = command(
        "import-history",
        "Explicitly import a bounded sourced history archive.",
        "crypto-market-analysis import-history --file history.csv --source kraken --quote EUR",
    )
    history.add_argument("--file", type=Path, required=True)
    history.add_argument("--member", help="Explicit ZIP member name when needed")
    history.add_argument("--expected-sha256", help="Optional expected archive SHA-256")
    calendar = command(
        "import-calendar",
        "Import an explicitly sourced calendar file.",
        "crypto-market-analysis import-calendar --file calendar.ics --kind bls",
    )
    calendar.add_argument("--file", type=Path, required=True)
    calendar.add_argument("--kind", choices=("bls", "fed", "ecb-manual"), default="bls",
                          help="ecb-manual requires a sourced manual JSON schedule, not an official feed")
    command(
        "collect-calendar",
        "Collect the bounded verified public calendar source once.",
        "crypto-market-analysis collect-calendar --mode live",
    )
    for p in (context, windows, comparison, plan_evaluation, issue, assessment, observe, vigilance,
              accumulation, preview, sensitivity, journal_summary, scorecard, budget, purchase_costs,
              disagreement, quote_context, derivatives, replay):
        p.add_argument("--format", choices=("json", "markdown"), default="json")
        p.add_argument("--output")
    for p in (brief, forecast, evaluation, chart, context, windows, issue, vigilance, history, accumulation):
        p.add_argument("--asset", type=validate_asset, default="BTC")
        p.add_argument("--source", help="Reference venue; otherwise the most usable complete history")
        p.add_argument("--quote", help="Native quote currency; no implicit conversion")
    return root


def main(argv: list[str] | None = None) -> int:
    with ExitStack() as resources:
        return _run_cli(argv, resources)


def _guard_configured_dataset(args, settings, resources):
    """Cover all configured CLI stores that can write or initialize a schema.

    Capacity reads and MCP startup have their separate noninitializing lifecycle;
    automatic repair already holds this same lock around its authoritative cycle.
    Legacy Engine commands can initialize schema even for an analytical request.
    """
    if not (settings.data_dir / "dataset.json").is_file():
        return
    from .analysis_cli import COMMANDS as ANALYSIS_COMMANDS
    from .capacity_cli import COMMANDS
    from .dataset_lock import DatasetBusy, dataset_writer_lock
    from .scalability_cli import COMMANDS as SCALABILITY_COMMANDS

    if getattr(args, "output", None):
        _protect_report_output(args, settings)
    if (args.command in COMMANDS | ANALYSIS_COMMANDS | SCALABILITY_COMMANDS or args.command in ("serve", "storage-policy", "verify-archive")
            or (args.command in ("maintain", "restore-archive") and not args.apply)):
        return
    try:
        resources.enter_context(dataset_writer_lock(settings.database))
    except DatasetBusy:
        return {"status": "busy", "reason": "dataset_writer_busy", "applied": False,
                "data_mode": args.mode, "action": args.command}


def _run_cli(argv, resources) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    try:
        if args.command == "init":
            if args.file.exists():
                raise ValueError(f"{args.file} already exists; choose another name.")
            args.file.parent.mkdir(parents=True, exist_ok=True)
            args.file.write_text(EXAMPLE, encoding="utf-8")
            write_json({"configuration": str(args.file.resolve())})
            return 0
        if args.command == "venues":
            write_json(
                {
                    "venues": [asdict(v) for v in VENUES.values()],
                    "notice": "A brand name alone does not establish a legal entity's MiCA status.",
                }
            )
            return 0
        if args.command == "sources":
            from .source_catalog import source_catalog

            write_json(source_catalog())
            return 0
        if args.command == "plan-init":
            from .plan_builder import construct_plan

            write_json(construct_plan(_json_file(args.request)), args.output)
            return 0
        if args.command == "plan-preview":
            from .plan_builder import preview_plan_funding

            now = args.as_of if args.as_of is not None else int(datetime.now(UTC).timestamp())
            _write_result(preview_plan_funding(_json_file(args.plan_config), now), args)
            return 0
        if args.command == "history-dataset":
            result = _history_operation(args)
            write_json(result, args.output)
            return 1 if result.get("status") == "busy" else 0
        if args.command in ("backup", "verify-backup", "restore-backup"):
            from .recovery import create_backup, restore_backup, verify_backup

            if args.as_of is not None:
                raise ValueError("Recovery operations preserve actual evidence; --as-of is for analysis commands.")
            if args.output:
                if args.command == "backup":
                    _protect_report_output(args, Settings(data_dir=args.database.parent))
                output = Path(args.output).resolve()
                protected = [Path(value).resolve() for value in
                             (getattr(args, "bundle", None), getattr(args, "destination", None)) if value is not None]
                if args.command == "backup":
                    database = args.database.resolve()
                    if output in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
                        raise ValueError("Recovery report output must not overwrite the source database or its sidecars.")
                    if args.quiescent_dataset:
                        protected.append(database.parent)
                if any(output == path or output.is_relative_to(path) for path in protected):
                    raise ValueError("Recovery report output must be outside source and destination bundles or datasets.")
            if args.command == "backup":
                database = args.database.expanduser().resolve()
                if args.quiescent_dataset and (database.parent / "dataset.json").is_file():
                    from .dataset_lock import DatasetBusy, dataset_writer_lock

                    try:
                        resources.enter_context(dataset_writer_lock(database))
                    except DatasetBusy:
                        write_json({"status": "busy", "reason": "dataset_writer_busy", "applied": False,
                                    "action": "backup"}, args.output)
                        return 1
                result = create_backup(args.database, args.destination, quiescent_dataset=args.quiescent_dataset)
            elif args.command == "verify-backup":
                result = verify_backup(args.bundle)
            else:
                result = restore_backup(args.bundle, args.destination)
            write_json(result, args.output)
            return 0
        if args.command == "budget-preview":
            from .budget import preview_budget_envelope

            _write_result(preview_budget_envelope(_json_file(args.request), bounded_as_of(args.as_of)), args)
            return 0
        if args.command == "purchase-costs":
            from .venue_analysis import analyze_purchase_costs

            _write_result(analyze_purchase_costs(_json_file(args.request), bounded_as_of(args.as_of)), args)
            return 0
        if args.command == "venue-disagreement":
            from .venue_analysis import analyze_venue_disagreement

            _write_result(analyze_venue_disagreement(_json_file(args.request), bounded_as_of(args.as_of)), args)
            return 0
        if args.command == "quote-context":
            from .quote_context import analyze_quote_context

            _write_result(analyze_quote_context(_json_file(args.request), bounded_as_of(args.as_of)), args)
            return 0
        if args.command == "derivatives-context":
            from .derivatives_context import analyze_derivatives_context

            _write_result(analyze_derivatives_context(_json_file(args.request), bounded_as_of(args.as_of)), args)
            return 0
        config_path = args.config or (Path("observer.toml") if Path("observer.toml").exists() else None)
        settings = load_settings(config_path, args.data_dir)
        busy = _guard_configured_dataset(args, settings, resources)
        if busy:
            write_json(busy, getattr(args, "output", None))
            return 1
        from .analysis_cli import COMMANDS as ANALYSIS_COMMANDS
        from .analysis_cli import analysis_exit_code, render_analysis_report, run_analysis_command
        from .capacity_cli import COMMANDS, capacity_exit_code, render_capacity_report, run_capacity_command
        from .scalability_cli import COMMANDS as SCALABILITY_COMMANDS
        from .scalability_cli import run_scalability_command, scalability_exit_code

        if args.command in SCALABILITY_COMMANDS:
            _protect_report_output(args, settings)
            request = _json_file(args.request) if args.request else None
            result = run_scalability_command(args, settings, request)
            if args.command == "freeze-validation" and args.output:
                from .scalability_cli import publish_protocol

                publish_protocol(result, Path(args.output))
            else:
                write_json(result, args.output)
            return scalability_exit_code(args.command, result, apply=getattr(args, "apply", False))

        if args.command in COMMANDS | ANALYSIS_COMMANDS:
            _protect_report_output(args, settings)
            request = _json_file(args.request) if getattr(args, "request", None) else None
            if args.command in ANALYSIS_COMMANDS:
                run_command, render_report, exit_code = run_analysis_command, render_analysis_report, analysis_exit_code
            else:
                run_command, render_report, exit_code = run_capacity_command, render_capacity_report, capacity_exit_code
            result = run_command(args, settings, request)
            if args.format == "json":
                write_json(result, args.output)
            else:
                text = render_report(result, args.command)
                if args.output:
                    path = Path(args.output)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                else:
                    print(text, end="")
            return exit_code(args.command, result, apply=getattr(args, "apply", False))
        if args.command in ("maintain", "storage-policy", "verify-archive", "restore-archive"):
            from types import SimpleNamespace

            from .storage import Store
            from .storage_policy import (
                StoragePolicy,
                inspect_storage,
                maintain_storage,
                restore_archive,
                verify_archive,
            )

            policy = StoragePolicy.from_settings(settings)
            if args.command == "maintain" and args.keep_days is not None and not 1 <= args.keep_days <= 365:
                raise ValueError("keep_days must be between 1 and 365.")
            if args.command == "verify-archive":
                checked = verify_archive(args.archive)
                write_json({key: value for key, value in checked.items() if key != "rows"})
            elif args.command == "storage-policy":
                write_json(inspect_storage(settings.database, policy), args.output)
            elif args.command == "restore-archive" and not args.apply:
                write_json(restore_archive(args.archive, None))
            else:
                if not settings.database.is_file():
                    raise ValueError("Maintenance requires an existing database; nothing was initialized.")
                store = Store(settings.database) if args.apply else SimpleNamespace(path=settings.database)
                store.storage_policy = policy
                if args.command == "restore-archive":
                    write_json(restore_archive(args.archive, store, apply=True))
                else:
                    result = maintain_storage(store, policy, apply=args.apply)
                    if args.apply:
                        from .retention import compact_liquidity

                        result["liquidity"] = compact_liquidity(store, args.keep_days if args.keep_days is not None else settings.liquidity_retention_days)
                    result["storage"] = inspect_storage(settings.database, policy)
                    write_json(result)
            return 0
        if args.command in ("capture-books", "collect-official-context"):
            if args.mode != "live" or args.as_of is not None:
                raise ValueError("Network capture requires --mode live without --as-of.")
        if args.command in ("capture-books", "collect-official-context", "qualify-sources", "local-workflow") or args.command.startswith("stored-"):
            _protect_report_output(args, settings)
        if args.command == "serve":
            from .server import create_server

            create_server(settings, args.mode, args.host, args.port).run(transport=args.transport)
            return 0
        engine = Engine(settings, args.mode)
        now = args.as_of if args.as_of is not None else int(datetime.now(UTC).timestamp())
        if args.command == "capture-books":
            result = engine.capture_books(sources=args.sources.split(","), assets=args.assets.split(","),
                                          quotes=args.quotes.split(","))
            _write_result(result, args)
            return 1 if result.get("status") == "error" else 0
        elif args.command == "collect-official-context":
            result = engine.collect_official_context(venues=args.venues.split(","),
                          include=args.include.split(","), max_requests=args.max_requests)
            _write_result(result, args)
            return 1 if result.get("status") == "error" else 0
        elif args.command.startswith("stored-"):
            request = _stored_request(args)
            operation = {"stored-derivatives": engine.stored_derivatives, "stored-quotes": engine.stored_quotes,
                         "stored-venue-costs": engine.stored_venues,
                         "stored-venue-disagreement": engine.stored_venues}[args.command]
            _write_result(operation(request, args.as_of), args)
        elif args.command == "qualify-sources":
            request = dict(_json_file(args.request))
            request.setdefault("data_mode", args.mode)
            _write_result(engine.qualify_sources(request, args.as_of), args)
        elif args.command == "local-workflow":
            from .local_workflow import run_local_workflow

            request = dict(_json_file(args.request))
            request.setdefault("data_mode", args.mode)
            if args.as_of is not None:
                if "as_of" in request and request["as_of"] != args.as_of:
                    raise ValueError("--as-of conflicts with the workflow JSON request.")
                request["as_of"] = args.as_of
            _write_result(run_local_workflow(settings, bounded_object(request)), args)
        elif args.command == "context":
            lookbacks = bounded_integers(_csv_numbers(args.lookbacks, int), "lookbacks", 3650, 16)
            _write_result(engine.context(args.asset, args.source, args.quote, args.as_of, lookbacks), args)
        elif args.command == "accumulation-brief":
            _write_result(engine.accumulation_brief(args.asset, args.source, args.quote, args.as_of,
                _json_file(args.plan_config) if args.plan_config else None), args)
        elif args.command == "cost-sensitivity":
            cases = _json_file(args.cost_cases)
            if set(cases) != {"cases"}:
                raise ValueError('Cost assumptions require one object with a "cases" array.')
            _write_result(engine.cost_sensitivity(_json_file(args.plan_config), cases["cases"],
                args.as_of, args.validation), args)
        elif args.command == "journal-summary":
            _write_result(engine.journal_summary(args.as_of, args.limit), args)
        elif args.command == "journal-scorecard":
            _write_result(engine.journal_scorecard(bounded_as_of(args.as_of), args.limit), args)
        elif args.command == "replay-assessment":
            _write_result(engine.replay_assessment(args.id), args)
        elif args.command == "windows":
            horizons = bounded_integers(_csv_numbers(args.horizons, int), "horizons", 365, 16)
            thresholds = bounded_thresholds(_csv_numbers(args.thresholds, float))
            _write_result(
                engine.purchase_windows(
                    args.asset, args.source, args.quote, args.as_of, horizons, thresholds
                ),
                args,
            )
        elif args.command == "compare-plans":
            _write_result(
                engine.compare_plans(
                    _json_file(args.plan_config), as_of=args.as_of, validation=args.validation
                ),
                args,
            )
        elif args.command == "evaluate-plans":
            origins = bounded_origins(_csv_numbers(args.origins, parse_time))
            _write_result(
                engine.evaluate_plans(
                    _json_file(args.plan_config),
                    as_of=args.as_of,
                    origins=origins,
                    validation=args.validation,
                    held_out_start=args.held_out_start,
                    policy_frozen_at=args.policy_frozen_at,
                ),
                args,
            )
        elif args.command == "issue":
            result = engine.issue_assessment(
                args.idempotency_key,
                asset=args.asset,
                source=args.source,
                quote=args.quote,
                as_of=args.as_of,
                plan_config=_json_file(args.plan_config) if args.plan_config else None,
                validation_protocol=_json_file(args.validation_protocol) if args.validation_protocol else None,
            )
            _write_result(result, args)
        elif args.command == "assessment":
            result = engine.assessment(args.id)
            _write_result({**result, "outcomes": engine.assessment_outcomes(args.id)}, args)
        elif args.command == "observe":
            _write_result(engine.observe_assessment(args.id, as_of=args.as_of), args)
        elif args.command == "vigilance":
            arguments = dict(
                asset=args.asset,
                source=args.source,
                quote=args.quote,
                as_of=args.as_of,
                horizon_hours=args.horizon_hours,
                policy=_json_file(args.policy) if args.policy else None,
            )
            result = (
                engine.record_vigilance(args.state_key, **arguments)
                if args.state_key
                else engine.vigilance(**arguments)
            )
            _write_result(result, args)
        elif args.command == "import-history":
            if not args.source or not args.quote:
                raise ValueError("History import requires explicit --source and --quote.")
            write_json(
                engine.import_history(
                    args.file,
                    args.source,
                    args.asset,
                    args.quote,
                    member=args.member,
                    expected_sha256=args.expected_sha256,
                )
            )
        elif args.command == "import-calendar":
            write_json(engine.import_calendar(args.file, kind=args.kind))
        elif args.command == "collect-calendar":
            if args.mode != "live" or args.as_of is not None:
                raise ValueError("Network collection requires --mode live without --as-of.")
            write_json(engine.collect_calendar())
        elif args.command == "collect":
            from .pipeline import collect

            if args.mode != "live" or args.as_of is not None:
                raise ValueError("Network collection requires --mode live without --as-of.")
            if args.venues:
                settings.venues = args.venues.split(",")
                if set(settings.venues) - set(VENUES):
                    raise ValueError("Unknown venue. Example: --venues kraken,coinbase")
            if args.quotes:
                quotes = args.quotes.split(",")
                if len(set(quotes)) != len(quotes) or any(q not in ("EUR", "USD", "USDT", "USDC") for q in quotes):
                    raise ValueError("Use distinct native EUR, USD, USDT or USDC quotes.")
                settings.quote_currencies = quotes
            if args.days is not None and not 1 <= args.days <= 3650:
                raise ValueError("--days must be between 1 and 3650.")
            if args.max_pages is not None:
                if not 1 <= args.max_pages <= 100:
                    raise ValueError("--max-pages must be between 1 and 100.")
                settings.max_pages = args.max_pages
            tasks = tuple(args.tasks.split(","))
            if not tasks or set(tasks) - {"market", "liquidity", "options", "context"}:
                raise ValueError("Unknown task; choose market,liquidity,options,context.")
            result = collect(settings, engine.store, tasks=tasks, days=args.days)
            write_json(result)
            return 1 if result["status"] == "error" else 0
        if args.command == "run":
            from .pipeline import run_scheduler

            if args.mode != "live" or args.as_of is not None:
                raise ValueError("The continuous collector requires --mode live without --as-of.")
            run_scheduler(settings, engine.store)
        elif args.command == "status":
            write_json(engine.status(args.as_of))
        elif args.command == "brief":
            bundle = engine.bundle(args.asset, args.source, args.quote, args.as_of)
            if args.format == "json":
                write_json(bundle, args.output)
            else:
                text = render_brief(bundle, args.cadence)
                if args.output:
                    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.output).write_text(text, encoding="utf-8")
                else:
                    print(text)
        elif args.command == "forecast":
            horizons = [int(h) for h in args.horizons.split(",")]
            if len(horizons) > 10 or any(h < 1 or h > 60 for h in horizons):
                raise ValueError("Choose at most ten horizons between 1 and 60 days.")
            write_json(
                engine.predictions(args.asset, horizons, args.threshold, args.source, args.quote, args.as_of),
                args.output,
            )
        elif args.command == "evaluate":
            if not 1 <= args.horizon <= 60:
                raise ValueError("The horizon must be between 1 and 60 days.")
            write_json(
                engine.evaluation(
                    args.asset,
                    args.horizon,
                    args.threshold,
                    args.source,
                    args.quote,
                    args.as_of,
                    args.validation,
                ),
                args.output,
            )
        elif args.command == "chart":
            if args.kind == "plans":
                from .charts import render_result_chart

                if args.plan_config is None:
                    raise ValueError("A plans chart requires --plan-config.")
                result = engine.compare_plans(
                    _json_file(args.plan_config), as_of=args.as_of, validation=args.validation
                )
                path = render_result_chart("plans", result, args.output)
            else:
                path = engine.chart(
                    args.kind,
                    args.asset,
                    args.output,
                    args.source,
                    args.quote,
                    args.as_of,
                    args.horizon,
                    args.validation,
                )
            write_json({"path": str(path.resolve()), "data_mode": args.mode})
        elif args.command == "export":
            write_json(engine.store.export_parquet(args.output))
        elif args.command == "demo":
            from .demo import seed_demo

            write_json(seed_demo(engine.store, args.days, now, tuple(args.quotes.split(","))))
        elif args.command == "import-events":
            if args.file.stat().st_size > 2_000_000:
                raise ValueError("Calendar too large (2 MB limit).")
            events = json.loads(args.file.read_text(encoding="utf-8"))
            if not isinstance(events, list) or len(events) > 1000:
                raise ValueError("Expected a list of at most 1000 events.")
            for event in events:
                if isinstance(event.get("event_time"), str):
                    event["event_time"] = parse_time(event["event_time"])
            write_json({"inserted": engine.store.put_events(events, now), "origin": "manual_import"})
        elif args.command == "import-candles":
            if args.file.stat().st_size > 100_000_000:
                raise ValueError("Import limited to 100 MB per file.")
            rows = []
            with args.file.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    timestamp = parse_time(row["open_time"])
                    rows.append(
                        Candle(
                            args.source,
                            args.asset,
                            args.quote,
                            "1h",
                            timestamp,
                            timestamp + 3600,
                            *[float(row[k]) for k in ("open", "high", "low", "close", "volume")],
                            now,
                            float(row["quote_volume"]) if row.get("quote_volume") else None,
                            float(row["taker_buy_quote_volume"])
                            if row.get("taker_buy_quote_volume")
                            else None,
                            "import",
                        )
                    )
            write_json({"inserted": engine.store.put_candles(rows), "data_mode": "import"})
        elif args.command == "import-metrics":
            from .models import Observation

            if args.file.stat().st_size > 20_000_000:
                raise ValueError("Metric import limited to 20 MB.")
            rows = []
            with args.file.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if not row.get("source_url", "").startswith("https://"):
                        raise ValueError("Each imported metric requires an HTTPS source_url.")
                    rows.append(
                        Observation(
                            row["source"],
                            row["asset"],
                            row["metric"],
                            parse_time(row["event_time"]),
                            now,
                            float(row["value"]),
                            row["unit"],
                            row.get("quote") or None,
                            {
                                "source_url": row["source_url"],
                                "origin": "manual_import",
                                "historical_known_at": "unknown",
                            },
                            "import",
                        )
                    )
            write_json({"inserted": engine.store.put_observations(rows), "data_mode": "import"})
        return 0
    except KeyboardInterrupt:
        print("Shutdown requested.", file=sys.stderr)
        return 130
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(
            json.dumps(
                {"error": str(exc), "hint": "Use crypto-market-analysis <command> --help."},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
