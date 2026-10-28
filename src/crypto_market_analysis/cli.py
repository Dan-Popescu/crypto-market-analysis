"""Non-interactive CLI; stdout is JSON or the requested report, logs use stderr."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import load_settings
from .engine import Engine, render_brief
from .models import Candle
from .reporting import (
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
venues = ["binance", "coinbase", "kraken", "okx", "bybit", "bitstamp", "bitfinex", "kucoin", "gate", "cryptocom", "gemini", "bitget", "mexc", "htx"]
history_days = 30
max_pages = 20
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
    if path.stat().st_size > 2_000_000:
        raise ValueError("Configuration exceeds the 2 MB limit.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
        description="BTC/ETH analysis using free public data.",
        epilog="Examples: crypto-market-analysis demo ; crypto-market-analysis brief --mode demo ; crypto-market-analysis collect --venues kraken,coinbase",
    )
    root.add_argument("--version", action="version", version=__version__)
    _common(root)
    subs = root.add_subparsers(dest="command", required=True)

    def command(name: str, description: str, example: str):
        p = subs.add_parser(name, help=description, description=description, epilog=f"Example: {example}")
        _common(p, True)
        return p

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
    maintenance.add_argument(
        "--keep-days", type=int, help="Days of detail to retain; defaults to TOML configuration"
    )
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
    candles.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
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
    for p in (comparison, plan_evaluation):
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
    assessment = command(
        "assessment",
        "Read an issued local assessment and appended outcomes.",
        "crypto-market-analysis assessment --id ASSESSMENT_ID",
    )
    assessment.add_argument("--id", required=True)
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
    calendar.add_argument("--kind", choices=("bls", "fed"), default="bls")
    command(
        "collect-calendar",
        "Collect the bounded verified public calendar source once.",
        "crypto-market-analysis collect-calendar --mode live",
    )
    for p in (context, windows, comparison, plan_evaluation, issue, assessment, observe, vigilance):
        p.add_argument("--format", choices=("json", "markdown"), default="json")
        p.add_argument("--output")
    for p in (brief, forecast, evaluation, chart, context, windows, issue, vigilance, history):
        p.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
        p.add_argument("--source", help="Reference venue; otherwise the most usable complete history")
        p.add_argument("--quote", help="Native quote currency; no implicit conversion")
    return root


def main(argv: list[str] | None = None) -> int:
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
        config_path = args.config or (Path("observer.toml") if Path("observer.toml").exists() else None)
        settings = load_settings(config_path, args.data_dir)
        engine = Engine(settings, args.mode)
        now = args.as_of if args.as_of is not None else int(datetime.now(UTC).timestamp())
        if args.command == "context":
            lookbacks = bounded_integers(_csv_numbers(args.lookbacks, int), "lookbacks", 3650, 16)
            _write_result(engine.context(args.asset, args.source, args.quote, args.as_of, lookbacks), args)
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
        elif args.command == "maintain":
            from .retention import compact_liquidity

            write_json(
                compact_liquidity(
                    engine.store,
                    args.keep_days if args.keep_days is not None else settings.liquidity_retention_days,
                )
            )
        elif args.command == "demo":
            from .demo import seed_demo

            write_json(seed_demo(engine.store, args.days, now))
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
        elif args.command == "serve":
            from .server import create_server

            create_server(settings, args.mode, args.host, args.port).run(transport=args.transport)
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
