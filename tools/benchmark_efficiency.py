"""Bounded, synthetic, offline baseline/treatment resource measurements.

Each case runs in its own subprocess and temporary dataset. Timings describe this
machine and fixture, not a production service objective. No network is permitted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import resource
import socket
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Event
from unittest.mock import patch

BASELINE = "bca6a39091e4b9f0d5bebbf6519b8b14bd208607"
CASES = ("refresh", "mcp", "history", "scheduler", "retention")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timed(function, repetitions):
    samples, result = [], None
    for _ in range(repetitions):
        started = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return result, {"samples_ms": samples, "p50_ms": statistics.median(samples),
                    "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1]}


def block_network():
    def denied(*args, **kwargs):
        raise RuntimeError("Network disabled by the synthetic efficiency harness")
    socket.create_connection = denied
    socket.socket.connect = denied


class SyntheticExchange:
    """A deterministic CCXT-shaped public adapter; traces actual requested cursors."""

    has = {"fetchOHLCV": True}

    def __init__(self):
        self.requests = []
        self.catalogues = 0

    def load_markets(self, reload=False):
        self.catalogues += 1
        return {"BTC/EUR": {"symbol": "BTC/EUR", "base": "BTC", "quote": "EUR",
                            "spot": True, "active": True}}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        latest = int(time.time()) // 3600 * 3600 - 3600
        start = max(latest - 30 * 86400, math.ceil(since / 3600000) * 3600)
        times = list(range(start, latest + 1, 3600))[:limit]
        self.requests.append({"symbol": symbol, "timeframe": timeframe, "since_ms": since,
                              "limit": limit, "returned_rows": len(times)})
        return [[t * 1000, 100, 102, 99, 101, 2] for t in times]

    def close(self):
        pass


def refresh_case(folder, args):
    from crypto_market_analysis.collectors import ExchangeClients
    from crypto_market_analysis.config import Settings
    from crypto_market_analysis.local_workflow import run_local_workflow
    from crypto_market_analysis.storage import Store

    settings = Settings(data_dir=folder, venues=["kraken"], assets=["BTC"],
                        quote_currencies=["EUR"], options=False, context=False, max_pages=3)
    store = Store(settings.database)
    adapter = SyntheticExchange()
    request = {"refresh": True, "sources": ["kraken"], "markets": ["BTC/EUR"],
               "required_history_days": 31, "max_pages": 3, "analyses": ["derivatives"]}
    cycles = []
    with ExchangeClients() as clients:
        clients._clients["kraken"] = adapter
        for _ in range(2):
            before = len(adapter.requests)
            started = time.perf_counter()
            report = run_local_workflow(settings, request, clients=clients)
            calls = adapter.requests[before:]
            cycles.append({"elapsed_ms": (time.perf_counter() - started) * 1000,
                           "requests": calls, "rows_returned": sum(c["returned_rows"] for c in calls),
                           "logical_operations": report["collection"]["budget"]["logical_operations"],
                           "stored_candles": store.counts()["candles"]})
        before = len(adapter.requests)
        try:
            run_local_workflow(settings, {**request, "backfill_days": 30}, clients=clients)
            backfill = {"supported": True, "requests": adapter.requests[before:]}
        except ValueError as exc:
            backfill = {"supported": False, "reason": str(exc)}
    return {"cycles": cycles, "explicit_backfill": backfill,
            "catalogue_loads": adapter.catalogues, "http_requests": 0,
            "logical_calls_are_not_http_counts": True}


async def mcp_request(server, tool, arguments):
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = server._mcp_server.request_handlers[CallToolRequest]
    response = await handler(CallToolRequest(method="tools/call",
                                            params=CallToolRequestParams(name=tool, arguments=arguments)))
    result = response.root
    if result.isError:
        raise ValueError(result.content[0].text)
    return result.structuredContent, len(result.model_dump_json(by_alias=True, exclude_none=True).encode())


async def reconstruct_mcp_result(server, metadata):
    """Reassemble root pages and oversize-row fragments using the public tool."""
    selection = {key: metadata[key] for key in ("handle", "selection_hash", "content_hash")}
    items, offset, page_count, maximum_bytes = [], 0, 0, 0
    while True:
        fragments, fragment_offset = [], 0
        while True:
            page, wire_bytes = await mcp_request(server, "get_result_page",
                                                {**selection, "offset": offset, "limit": 100,
                                                 "fragment_offset": fragment_offset})
            page_count += 1
            maximum_bytes = max(maximum_bytes, wire_bytes)
            if "fragment" not in page:
                items.extend(page["items"])
                break
            fragments.append(page["fragment"])
            fragment_offset = page["next_fragment_offset"]
            if fragment_offset is None:
                items.append(json.loads("".join(fragments)))
                break
            if page_count > 2000:
                raise AssertionError("Result fragment fixture bound exceeded")
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
        if page_count > 2000:
            raise AssertionError("Result page fixture bound exceeded")
    result = {item["key"]: item["value"] for item in items}
    return result, page_count, maximum_bytes


def oi(timestamp, index):
    from crypto_market_analysis.models import Observation

    return Observation("venue", "BTC", "open_interest_amount", timestamp, timestamp, 1000 + index / 10,
                       "BTC", "USDT", {"symbol": "BTC/USDT:USDT", "settle": "USDT", "market_type": "swap",
                                       "counting_convention": "one_sided"}, "import")


def mcp_case(folder, args):
    from crypto_market_analysis.storage import Store
    from crypto_market_analysis.stored_analysis import build_stored_derivatives_inputs

    now = args.fixture_now
    store = Store(folder / "observer.sqlite")
    store.put_observations([oi(now - (999 - index) * 90, index) for index in range(1000)])
    request = {"asset": "BTC", "source": "venue", "quote": "USDT", "data_mode": "import"}
    built = build_stored_derivatives_inputs(store, request, now)
    expected_records = built["stored_inputs"]["consumed_records"]
    from crypto_market_analysis.config import Settings
    from crypto_market_analysis.server import create_server

    server = create_server(Settings(data_dir=folder))

    async def measure():
        started = time.perf_counter()
        result, wire_bytes = await mcp_request(server, "get_stored_derivatives", {"request": request, "as_of": now})
        elapsed_ms = (time.perf_counter() - started) * 1000
        metadata = result.get("_mcp_result")
        if metadata:
            full, pages, max_page_bytes = await reconstruct_mcp_result(server, metadata)
        else:
            full, pages, max_page_bytes = result, 0, 0
        return {"input_rows": 1000, "serialized_call_tool_result_bytes": wire_bytes,
                "tool_elapsed_ms": elapsed_ms, "under_32_kib": wire_bytes <= 32768,
                "serialization_scope": "Registered MCP request handler and SDK CallToolResult; no transport framing",
                "full_result_sha256": digest(full), "financial_series_sha256": digest(full["series"]),
                "expected_records_sha256": digest(expected_records),
                "reassembled_records_sha256": digest(full["stored_inputs"]["consumed_records"]),
                "all_financial_values_preserved": digest(full["stored_inputs"]["consumed_records"]) == digest(expected_records),
                "paged_result_hash_matches_handle": digest(full) == metadata["content_hash"] if metadata else None,
                "page_calls": pages, "maximum_page_bytes": max_page_bytes,
                "all_pages_under_32_kib": max_page_bytes <= 32768,
                "financial_series": full["series"]}

    return asyncio.run(measure())


@contextmanager
def trace_store(store, traces):
    original = store.connection

    @contextmanager
    def connection():
        with original() as con:
            con.set_trace_callback(traces.append)
            yield con
    store.connection = connection
    try:
        yield
    finally:
        store.connection = original


def history_case(folder, args):
    from crypto_market_analysis.models import Candle, Observation
    from crypto_market_analysis.storage import Store

    now = args.fixture_now
    store = Store(folder / "observer.sqlite")
    setup_start = time.perf_counter()
    # All unrelated observations are newer than the requested native series.
    for start in range(0, args.unrelated_rows, 5000):
        store.put_observations([Observation("busy", "BTC", "spread_bps", now - index, now,
                                            index / 100, "bps", "EUR", data_mode="import")
                                for index in range(start, min(start + 5000, args.unrelated_rows))])
    selected = [Observation("sparse", "BTC", "open_interest_amount", now - args.unrelated_rows - i - 1,
                            now, float(i), "BTC", "USDT", {"symbol": "BTC/USDT:USDT"}, "import")
                for i in range(args.selected_rows)]
    store.put_observations(selected)
    opened = now // 3600 * 3600 - (args.selected_rows + 1) * 3600
    store.put_candles([Candle("sparse", "BTC", "EUR", "1h", opened + i * 3600,
                              opened + (i + 1) * 3600, 100, 102, 99, 101, 2, now, data_mode="import")
                       for i in range(args.selected_rows)])
    setup_ms = (time.perf_counter() - setup_start) * 1000
    traces = []
    if hasattr(store, "metric_history"):
        def read():
            return store.metric_history("BTC", "open_interest_amount", source="sparse", limit=100,
                                        mode="import", as_of=now)
    else:
        def read():
            rows = [asdict(row) for row in store.observations("BTC", mode="import", as_of=now)
                    if row.metric == "open_interest_amount" and row.source == "sparse"]
            return {"points": rows[:100], "truncated": len(rows) > 100}
    with trace_store(store, traces):
        result, timing = timed(read, args.repetitions)
    plans = []
    with store.connection() as con:
        for sql in dict.fromkeys(traces):
            if sql.lstrip().upper().startswith(("SELECT", "WITH")):
                plans.append({"sql": sql, "query_plan": [list(row) for row in con.execute("EXPLAIN QUERY PLAN " + sql)]})
    first_point_count = len(result["points"])
    pages, points = 1, list(result["points"])
    cursor = result.get("next_cursor")
    while cursor:
        result = store.metric_history("BTC", "open_interest_amount", source="sparse", limit=100,
                                      mode="import", as_of=now, cursor=cursor)
        points.extend(result["points"])
        pages += 1
        cursor = result.get("next_cursor")
        if pages > args.selected_rows + 1 or len(points) > args.selected_rows:
            raise AssertionError("History cursor did not advance within the fixture row bound")
    expected = [asdict(row) for row in selected]
    candle_rows, candle_timing = timed(lambda: store.candles("BTC", source="sparse", quote="EUR",
                                                            mode="import", as_of=now), args.repetitions)
    sizes = {"database_bytes": store.path.stat().st_size,
             "wal_bytes": store.path.with_name(store.path.name + "-wal").stat().st_size
             if store.path.with_name(store.path.name + "-wal").exists() else 0}
    return {"counts": store.counts(), "setup_ms": setup_ms, "history_timing": timing,
            "candle_timing": candle_timing, "selected_candles_returned": len(candle_rows),
            "expected_selected_points": len(expected), "returned_points": len(points), "pages": pages,
            "all_selected_values_preserved": digest(points) == digest(expected),
            "selected_rows_sha256": digest(expected), "returned_rows_sha256": digest(points),
            "first_point_count": first_point_count, "executed_queries": plans, **sizes}


def scheduler_case(folder, args):
    from crypto_market_analysis.collectors import ExchangeClients
    from crypto_market_analysis.config import Settings
    from crypto_market_analysis.models import CollectionResult
    from crypto_market_analysis.pipeline import _run_scheduler
    from crypto_market_analysis.pipeline import collect as real_collect
    from crypto_market_analysis.storage import Store

    settings = Settings(data_dir=folder, venues=["kraken"], assets=["BTC"],
                        quote_currencies=["EUR"], options=False, context=False)
    store = Store(settings.database)
    now = int(time.time())
    store.record_run(CollectionResult("kraken"), "market+liquidity", now - 1)
    store.record_run(CollectionResult("storage"), "retention", now - 1)
    for i in range(250):
        store.record_run(CollectionResult(f"unrelated-{i}"), "other", now)
    calls = []

    def collect(*args, **kwargs):
        calls.append({"tasks": kwargs.get("tasks"), "source_tasks": kwargs.get("source_tasks")})
        return real_collect(*args, **kwargs)

    class Once(Event):
        def wait(self, timeout=None):
            self.set()
            return True

    adapter = SyntheticExchange()
    with ExchangeClients() as clients, patch("crypto_market_analysis.pipeline.collect", collect):
        clients._clients["kraken"] = adapter
        started = time.perf_counter()
        _run_scheduler(settings, store, Once(), clients)
    return {"persisted_runs": store.counts()["runs"], "unrelated_newer_run_series": 250,
            "unnecessary_collection_calls": len(calls), "calls": calls,
            "unnecessary_source_calls": len(adapter.requests) + adapter.catalogues,
            "source_fetch_requests": adapter.requests, "catalogue_loads": adapter.catalogues,
            "iteration_ms": (time.perf_counter() - started) * 1000,
            "limitation": "Synthetic CCXT adapter records actual fetch/catalogue calls; no network requests."}


def retention_case(folder, args):
    try:
        from crypto_market_analysis.storage_policy import (
            StoragePolicy,
            inspect_storage,
            maintain_storage,
            restore_archive,
            verify_archive,
        )
    except ImportError:
        return {"status": "unavailable", "reason": "Baseline has no storage lifecycle policy API"}
    from crypto_market_analysis.book_snapshots import retain_books, retained_book
    from crypto_market_analysis.models import CollectionResult
    from crypto_market_analysis.storage import Store

    store = Store(folder / "observer.sqlite")
    policy = StoragePolicy()
    for index in range(20):
        book = retained_book({"bids": [["100.01", "2.5"]], "asks": [["101.01", "3.5"]]},
                             source="synthetic", asset="BTC", quote="EUR", symbol="BTC/EUR",
                             captured_at=index + 1, requested_limit=1, data_mode="import")
        retain_books(store, [book])
        store.record_run(CollectionResult("synthetic"), "benchmark", index + 1)
    now = 40 * 86400
    before = inspect_storage(store.path, policy)
    dry_run = maintain_storage(store, policy, apply=False, now=now)
    after_dry_run = inspect_storage(store.path, policy)
    applied = maintain_storage(store, policy, apply=True, now=now)
    archives = [Path(row["archive"]) for row in applied["families"] if row["archive"]]
    verified = [verify_archive(path) for path in archives]
    store.storage_policy = StoragePolicy(hot_max_bytes=1)
    try:
        store.record_run(CollectionResult("quota-probe"), "benchmark", now)
        guard = {"write_refused": False}
    except ValueError as exc:
        guard = {"write_refused": True, "reason": str(exc)}
    store.storage_policy = policy
    restore = [restore_archive(path, store, apply=True) for path in archives]
    restored = inspect_storage(store.path, policy)
    # New evidence forces a new archive, rather than reusing a verified archive.
    retain_books(store, [retained_book({"bids": [["100", "1"]], "asks": [["101", "1"]]},
                                      source="quota-probe", asset="BTC", quote="EUR", symbol="BTC/EUR",
                                      captured_at=1, requested_limit=1, data_mode="import")])
    guard_inventory = inspect_storage(store.path, policy)["families"]
    try:
        maintain_storage(store, StoragePolicy(archive_max_bytes=1), apply=True, now=now)
        archive_guard = {"archive_refused": False}
    except ValueError as exc:
        archive_guard = {"archive_refused": True, "reason": str(exc)}
    after_guard = inspect_storage(store.path, policy)
    archive_guard["hot_evidence_preserved"] = after_guard["families"] == guard_inventory
    return {"status": "measured", "dry_run_preserved_inventory": before == after_dry_run,
            "dry_run": dry_run, "applied": applied,
            "verified_archive_rows": sum(len(item["rows"]) for item in verified),
            "archive_bytes": sum(item["bytes"] for item in verified), "quota_guard": guard,
            "archive_budget_guard": archive_guard,
            "restore": restore, "before_families": before["families"],
            "restored_families": restored["families"], "physical_after": restored["physical"]}


def worker(args):
    block_network()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="cma-efficiency-data-") as directory:
        result = globals()[args.worker + "_case"](Path(directory), args)
    # Linux reports KiB; macOS reports bytes.
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"case": args.worker, "elapsed_ms": (time.perf_counter() - started) * 1000,
            "peak_rss_bytes": peak_rss if sys.platform == "darwin" else peak_rss * 1024, **result}


def bounded_int(low, high):
    def parse(value):
        parsed = int(value)
        if not low <= parsed <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return parsed
    return parse


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Examples:\n  uv run --frozen python tools/benchmark_efficiency.py --output /tmp/efficiency.json\n"
                                            "  uv run --frozen python tools/benchmark_efficiency.py --cases refresh mcp scheduler")
    parser.add_argument("--baseline", default=BASELINE, help="Local immutable commit to compare (no fetch)")
    parser.add_argument("--output", type=Path, help="Write report outside the repository; otherwise JSON to stdout")
    parser.add_argument("--cases", choices=CASES, nargs="+", default=list(CASES))
    parser.add_argument("--unrelated-rows", type=bounded_int(0, 220000), default=210000)
    parser.add_argument("--selected-rows", type=bounded_int(1, 10000), default=10000)
    parser.add_argument("--repetitions", type=bounded_int(1, 10), default=5)
    parser.add_argument("--case-timeout", type=bounded_int(10, 60), default=60, help="Seconds per isolated case")
    parser.add_argument("--worker", choices=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-now", type=int, default=int(time.time()), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(canonical(worker(args)))
        return 0
    root = Path(__file__).resolve().parents[1]
    if args.output and args.output.resolve().is_relative_to(root):
        parser.error("--output must be outside the repository (for example /tmp/efficiency.json)")
    try:
        baseline = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options",
                                            args.baseline + "^{commit}"], cwd=root, text=True,
                                           stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        parser.error("--baseline must name an existing local commit; no remote fetch is performed")
    treatment = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--", "src"], cwd=root, text=True).strip())
    report = {"schema_version": 1, "baseline_commit": baseline, "treatment_commit": treatment,
              "treatment_source_dirty": dirty, "python": sys.version, "synthetic": True, "network_requests": 0,
              "limits": {"unrelated_rows": args.unrelated_rows, "selected_rows": args.selected_rows,
                         "repetitions": args.repetitions, "case_timeout_seconds": args.case_timeout,
                         "fixture_now": args.fixture_now},
              "limitations": ["Descriptive local measurements, not production latency or memory guarantees.",
                              "No real provider coverage, reuse permission, paid APIs or financial performance tested.",
                              "RSS includes Python imports and fixture setup; file sizes are SQLite logical files after reads."],
              "results": {}}
    with tempfile.TemporaryDirectory(prefix="cma-efficiency-baseline-") as directory:
        baseline_root = Path(directory)
        archive = subprocess.check_output(["git", "archive", baseline, "src"], cwd=root)
        import io
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(baseline_root, filter="data")
        for label, source in (("baseline", baseline_root), ("treatment", root)):
            report["results"][label] = {}
            for case in args.cases:
                print(f"Measuring {label}: {case}", file=sys.stderr, flush=True)
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", case,
                           "--unrelated-rows", str(args.unrelated_rows), "--selected-rows", str(args.selected_rows),
                           "--repetitions", str(args.repetitions), "--fixture-now", str(args.fixture_now)]
                try:
                    completed = subprocess.run(command, env={**os.environ, "PYTHONPATH": str(source / "src")},
                                               cwd=source, capture_output=True, text=True, timeout=args.case_timeout)
                    if completed.returncode:
                        raise RuntimeError(completed.stderr[-4000:])
                    report["results"][label][case] = json.loads(completed.stdout)
                except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as exc:
                    report["results"][label][case] = {"error": str(exc)}
    baseline_cases, treatment_cases = report["results"]["baseline"], report["results"]["treatment"]
    checks = {}
    if "refresh" in treatment_cases and "cycles" in treatment_cases["refresh"]:
        checks["default_refresh_reduces_refetched_rows"] = (treatment_cases["refresh"]["cycles"][1]["rows_returned"]
                                                           < baseline_cases["refresh"]["cycles"][1]["rows_returned"])
        checks["explicit_backfill_supported"] = treatment_cases["refresh"]["explicit_backfill"]["supported"]
    if "mcp" in treatment_cases and "under_32_kib" in treatment_cases["mcp"]:
        value = treatment_cases["mcp"]
        checks["mcp_summary_under_32_kib"] = value["under_32_kib"]
        checks["mcp_pages_preserve_full_hash"] = value["paged_result_hash_matches_handle"] is True
        checks["mcp_pages_under_32_kib"] = value["all_pages_under_32_kib"]
        checks["mcp_all_financial_values_preserved"] = value["all_financial_values_preserved"]
        checks["mcp_financial_result_matches_baseline"] = (value["financial_series_sha256"]
                                                          == baseline_cases["mcp"]["financial_series_sha256"])
    if "history" in treatment_cases and "all_selected_values_preserved" in treatment_cases["history"]:
        checks["selected_history_complete"] = treatment_cases["history"]["all_selected_values_preserved"]
    if "scheduler" in treatment_cases and "unnecessary_collection_calls" in treatment_cases["scheduler"]:
        checks["fresh_scheduler_does_not_dispatch_collection"] = treatment_cases["scheduler"]["unnecessary_collection_calls"] == 0
    if "retention" in treatment_cases:
        checks["storage_lifecycle_available"] = treatment_cases["retention"].get("status") == "measured"
    if "retention" in treatment_cases and treatment_cases["retention"].get("status") == "measured":
        value = treatment_cases["retention"]
        checks["storage_dry_run_preserves_inventory"] = value["dry_run_preserved_inventory"]
        checks["hot_budget_refuses_write"] = value["quota_guard"]["write_refused"]
        checks["archive_budget_refuses_without_evidence_loss"] = (value["archive_budget_guard"]["archive_refused"]
                                                                 and value["archive_budget_guard"]["hot_evidence_preserved"])
    report["verification"] = checks
    encoded = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
        print(str(args.output))
    else:
        print(encoded)
    return int(any("error" in result for cases in report["results"].values() for result in cases.values())
               or not all(checks.values()))


if __name__ == "__main__":
    raise SystemExit(main())
