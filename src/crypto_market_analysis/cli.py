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

    init = command("init", "Create an editable configuration.", "crypto-market-analysis init --file observer.toml")
    init.add_argument("--file", type=Path, default=Path("observer.toml"))
    command(
        "venues", "Venue catalogue and documented regulatory status.", "crypto-market-analysis venues"
    )
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
        choices=("price", "weekly", "liquidity", "leverage", "options", "calibration"),
        default="weekly",
    )
    chart.add_argument("--output", type=Path, required=True)
    chart.add_argument("--horizon", type=int, default=7)
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
        "serve", "MCP server using STDIO or Streamable HTTP.", "crypto-market-analysis serve --transport stdio"
    )
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    for p in (brief, forecast, evaluation, chart):
        p.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
        p.add_argument(
            "--source", help="Reference venue; otherwise the most usable complete history"
        )
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
        config_path = args.config or (Path("observer.toml") if Path("observer.toml").exists() else None)
        settings = load_settings(config_path, args.data_dir)
        engine = Engine(settings, args.mode)
        now = args.as_of or int(datetime.now(UTC).timestamp())
        if args.command == "collect":
            from .pipeline import collect

            if args.mode != "live" or args.as_of:
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

            if args.mode != "live" or args.as_of:
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
                {"error": str(exc), "hint": "Use crypto-market-analysis <command> --help."}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
