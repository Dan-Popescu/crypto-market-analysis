"""Bounded offline pipeline endurance experiment; accelerated time is not a real soak.

Run with PYTHONPATH=src python tools/benchmark_endurance.py --output /tmp/endurance.json.
Two finite worker processes share one temporary SQLite directory, exercising an
actual process restart. All generated financial evidence is explicitly demo data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(condition, message):
    """Never disable experimental correctness checks with Python's -O flag."""
    if not condition:
        raise AssertionError(message)


def distribution(samples):
    ordered = sorted(samples)
    return {"samples_ms": samples, "count": len(samples),
            "p50_ms": statistics.median(samples) if samples else None,
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] if samples else None}


@contextmanager
def block_network():
    """Fail closed for DNS, stream connections and connectionless socket sends."""
    attempts = []

    def denied(*_args, **_kwargs):
        attempts.append("blocked")
        raise RuntimeError("Network disabled by the synthetic endurance harness")

    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.socket.sendto"):
            stack.enter_context(patch(target, denied))
        yield attempts


class Clock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class OneIteration(Event):
    def wait(self, timeout=None):
        self.set()
        return True


class SyntheticExchange:
    """Deterministic public CCXT shape; adapter calls are never HTTP requests."""

    has = {"fetchOHLCV": True, "fetchOrderBook": True}

    def __init__(self, source, clock, slow_ms):
        self.source, self.clock, self.slow_ms = source, clock, slow_ms
        self.cycle = 0
        self.calls = []
        self.closed = False

    def load_markets(self, reload=False):
        self.calls.append({"kind": "catalogue", "reload": reload, "cycle": self.cycle})
        return {f"{a}/{q}": {"symbol": f"{a}/{q}", "base": a, "quote": q,
                              "spot": True, "active": True}
                for a in ("BTC", "ETH") for q in ("EUR", "USD")}

    def price(self, symbol):
        return (100 if symbol.startswith("BTC") else 10) * (1 if symbol.endswith("EUR") else 2)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        latest = int(self.clock.time()) // 3600 * 3600 - 3600
        start = max(latest - 24 * 3600, math.ceil(since / 3600000) * 3600)
        times = list(range(start, latest + 1, 3600))[:limit]
        call = {"kind": "ohlcv", "symbol": symbol, "cycle": self.cycle,
                "since_ms": since, "returned_rows": len(times)}
        self.calls.append(call)
        if self.source == "coinbase" and self.cycle % 4 == 1 and symbol == "ETH/USD":
            call["returned_rows"] = 0
            call["error"] = "RequestTimeout: synthetic partial source"
            raise TimeoutError("RequestTimeout: synthetic partial source")
        price = self.price(symbol)
        return [[t * 1000, price, price + 2, price - 1, price + 1, 2] for t in times]

    def fetch_order_book(self, symbol, limit):
        self.calls.append({"kind": "book", "symbol": symbol, "cycle": self.cycle})
        if self.source == "coinbase":
            # This intentionally small real delay allows readers and writes to overlap.
            # It never scales with virtual days and is included in measured latency.
            time.sleep(self.slow_ms / 1000)
        price = self.price(symbol)
        return {"timestamp": int(self.clock.time()) * 1000,
                "bids": [[price, 2], [price - .1, 3]],
                "asks": [[price + .1, 2], [price + .2, 3]]}

    def close(self):
        self.closed = True


def demo_store(path):
    """Fixture producer bridge: pipeline's live-only producer is explicitly demo here.

    All validation, SQLite writes, versioning and run recording remain real Store
    operations. No stored row is relabelled and no shared Store class is patched.
    """
    from crypto_market_analysis.storage import Store

    class DemoStore(Store):
        def save_result(self, result, task, now, *, data_mode=None):
            fixture = replace(result, candles=[replace(c, data_mode="demo") for c in result.candles],
                              observations=[replace(o, data_mode="demo") for o in result.observations],
                              books=[{**book, "data_mode": "demo"} for book in result.books])
            return super().save_result(fixture, task, now, data_mode="demo")

        def record_run(self, result, task, now, *, data_mode=None):
            return super().record_run(result, task, now, data_mode="demo")

    return DemoStore(path)


async def mcp_call(server, tool, arguments, samples, sizes, *, expect_error=False):
    from mcp.types import CallToolRequest, CallToolRequestParams

    started = time.perf_counter()
    handler = server._mcp_server.request_handlers[CallToolRequest]
    response = await handler(CallToolRequest(method="tools/call",
                                            params=CallToolRequestParams(name=tool, arguments=arguments)))
    result = response.root
    samples.append((time.perf_counter() - started) * 1000)
    size = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
    sizes.append(size)
    require(size <= 32768, f"MCP {tool} exceeded 32 KiB")
    if expect_error:
        require(result.isError, f"MCP {tool} unexpectedly accepted invalid/expired selection")
        return None
    require(not result.isError, f"MCP {tool}: {result.content[0].text if result.isError else ''}")
    return result.structuredContent


async def full_result(server, summary, samples, sizes):
    meta = summary["_mcp_result"]
    selection = {key: meta[key] for key in ("handle", "selection_hash", "content_hash")}
    items, offset, calls = [], 0, 0
    while True:
        fragments, fragment_offset = [], 0
        while True:
            page = await mcp_call(server, "get_result_page", {**selection, "offset": offset, "limit": 100,
                                  "fragment_offset": fragment_offset}, samples, sizes)
            calls += 1
            require(calls <= 128, "Frozen page/fragment bound exceeded")
            if "fragment" not in page:
                items.extend(page["items"])
                break
            fragments.append(page["fragment"])
            fragment_offset = page["next_fragment_offset"]
            if fragment_offset is None:
                items.append(json.loads("".join(fragments)))
                break
        if page["next_offset"] is None:
            break
        require(page["next_offset"] > offset, "Frozen page did not advance")
        offset = page["next_offset"]
    full = {item["key"]: item["value"] for item in items}
    require(digest(full) == meta["content_hash"], "Frozen pages changed full result")
    return full, calls


def resident_bytes():
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def seed(store, now):
    from crypto_market_analysis.book_snapshots import retain_books, retained_book
    from crypto_market_analysis.models import Observation

    # A stable selected series provides an exact oracle during unrelated live-path writes.
    store.put_observations([Observation("synthetic-oracle", "BTC", "open_interest_amount", now - 1000 + i,
                                       now - 1, float(i), "BTC", q,
                                       {"symbol": f"BTC/{q}", "synthetic": True, "padding": "x" * 120}, "demo")
                            for q in ("EUR", "USD") for i in range(60)])
    # A future receipt and a future event must both be excluded by the cutoff.
    store.put_observations([Observation("synthetic-oracle", "BTC", "open_interest_amount", event, receipt,
                                       99999., "BTC", "EUR", {"symbol": "BTC/EUR"}, "demo")
                            for event, receipt in ((now - 1, now + 40 * 86400),
                                                   (now + 40 * 86400, now + 40 * 86400))])
    retain_books(store, [retained_book({"bids": [["100", "2"]], "asks": [["101", "2"]]},
                                      source="synthetic-oracle", asset="BTC", quote="EUR", symbol="BTC/EUR",
                                      captured_at=now - 3 * 86400, requested_limit=1, data_mode="demo")])


def durable_states(store):
    with store.connection() as con:
        return [dict(row) for row in con.execute("SELECT * FROM scheduler_state ORDER BY source,task")]


def run_phase(folder, args):
    from crypto_market_analysis import collectors, pipeline, retention, storage, storage_policy
    from crypto_market_analysis.collectors import ExchangeClients
    from crypto_market_analysis.config import Settings
    from crypto_market_analysis.mcp_results import MCPResultPresenter, ResultCache
    from crypto_market_analysis.server import create_server

    started = time.perf_counter()
    total = min(args.virtual_days * args.cycles_per_day + 1, args.max_cycles)
    midpoint = total // 2
    first, end = (0, midpoint) if args.phase == 1 else (midpoint, total)
    step = args.virtual_days * 86400 / (total - 1)
    clock = Clock(args.fixture_now + first * step)
    settings = Settings(data_dir=folder, assets=["BTC", "ETH"], quote_currencies=["EUR", "USD"],
                        venues=["kraken", "coinbase"], options=False, context=False,
                        history_days=1, max_pages=2, workers=2,
                        storage_maintenance_enabled=True, storage_book_keep_days=1,
                        storage_run_keep_days=1, liquidity_retention_days=1)
    store = demo_store(settings.database)
    if args.phase == 1:
        seed(store, args.fixture_now)
    initial_candle_keys = {(c.source, c.asset, c.quote, c.open_time)
                           for asset in settings.assets for c in store.candles(asset, mode="demo",
                                                                              as_of=int(clock.time()))}
    cache = ResultCache(ttl=300, max_bytes=8 * 1024**2, max_entries=64,
                        clock=clock.monotonic, wall_clock=clock.time)
    presenter = MCPResultPresenter(cache)
    samples, sizes, read_batches, collect_samples, storage_samples, states = [], [], [], [], [], []
    lock = Lock()
    adapters = {source: SyntheticExchange(source, clock, args.slow_ms) for source in settings.venues}
    real_collect = pipeline.collect
    restart = {"performed": args.phase == 2}
    page_calls = []

    async def read_batch(server, cutoff, selected_quote):
        local_samples, local_sizes = [], []
        request = {"asset": "BTC", "metric": "open_interest_amount", "source": "synthetic-oracle",
                   "quote": selected_quote, "as_of": cutoff, "limit": 100}
        before = time.perf_counter()
        for _ in range(args.reads_per_cycle):
            expected = [asdict(p) for p in store.observations("BTC", mode="demo", as_of=cutoff)
                        if p.source == "synthetic-oracle" and p.quote == selected_quote]
            expected.sort(key=lambda p: p["event_time"], reverse=True)
            points, cursor, history_pages = [], None, 0
            while True:
                summary = await mcp_call(server, "get_metric_history", request | {"cursor": cursor},
                                         local_samples, local_sizes)
                full, pages = await full_result(server, summary, local_samples, local_sizes)
                page_calls.append(pages)
                points.extend(full["points"])
                history_pages += 1
                require(history_pages <= 16, "History cursor page bound exceeded")
                cursor = full["next_cursor"]
                if cursor is None:
                    break
            require(points == expected, "MCP history differs from stable SQLite oracle")
            require(len(points) == 60, "Future receipt/event leaked into the selected history")
            require(all(p["quote"] == selected_quote and p["data_mode"] == "demo"
                        and p["event_time"] <= cutoff and p["observed_at"] <= cutoff
                        for p in points), "Native quote, mode or known-at clock mismatch")
            context = await mcp_call(server, "get_market_context", {"asset": "BTC", "source": "kraken",
                                    "quote": selected_quote, "as_of": cutoff, "lookbacks": [1, 7]},
                                    local_samples, local_sizes)
            await full_result(server, context, local_samples, local_sizes)
        with lock:
            samples.extend(local_samples)
            sizes.extend(local_sizes)
            read_batches.append({"started": before, "completed": time.perf_counter()})

    def observed_collect(*positional, **kwargs):
        cutoff = int(clock.time())
        before = time.perf_counter()
        futures = [readers.submit(asyncio.run, read_batch(server, cutoff, ("EUR", "USD")[i % 2]))
                   for i in range(args.readers)]
        result = real_collect(*positional, **kwargs)
        completed = time.perf_counter()
        collect_samples.append((completed - before) * 1000)
        for future in futures:
            future.result(timeout=15)
        collection_windows.append((before, completed))
        statuses.extend(row["status"] for row in result["reports"])
        return result

    collection_windows, statuses = [], []
    with ExitStack() as stack:
        for module in (pipeline, collectors, retention, storage, storage_policy):
            stack.enter_context(patch.object(module, "time", clock))
        stack.enter_context(patch("crypto_market_analysis.server.MCPResultPresenter", return_value=presenter))
        server = create_server(settings, mode="demo")
        with ExchangeClients(catalogue_ttl=86400) as clients, ThreadPoolExecutor(max_workers=args.readers) as readers:
            clients._clients.update(adapters)
            if args.phase == 2:
                checkpoint = json.loads((folder / "checkpoint.json").read_text())
                require(store.counts() == checkpoint["counts"], "Process restart lost stored rows")
                require(durable_states(store) == checkpoint["states"], "Process restart lost durable schedule")
                clock.now = checkpoint["clock"]
                before_counts, before_states = store.counts(), durable_states(store)
                pipeline._run_scheduler(settings, store, OneIteration(), clients)
                require(store.counts() == before_counts and durable_states(store) == before_states,
                        "Restart at a completed clock changed rows/schedule")
                require(not any(a.calls for a in adapters.values()), "Restart repeated a not-due source request")
                restart.update({"counts_match": True, "durable_schedule_match": True, "not_due_adapter_calls": 0,
                                "previous_pid": checkpoint["pid"], "new_pid": os.getpid()})
                require(checkpoint["pid"] != os.getpid(), "Restart did not use a new process")
            stack.enter_context(patch.object(pipeline, "collect", observed_collect))
            for cycle in range(first, end):
                clock.now = args.fixture_now + cycle * step
                for adapter in adapters.values():
                    adapter.cycle = cycle
                pipeline._run_scheduler(settings, store, OneIteration(), clients)
                inventory = storage_policy.inspect_storage(store.path)
                storage_samples.append({"cycle": cycle, "virtual_now": clock.now,
                                        "physical": inventory["physical"],
                                        "logical_payload_bytes": sum(v["payload_bytes"]
                                                                     for v in inventory["families"].values()),
                                        "counts": store.counts(), "rss_bytes": resident_bytes(),
                                        "warmup": cycle - first < 2})
                states.append({"cycle": cycle, "states": durable_states(store)})
            lifecycle = lifecycle_checks(store, settings, presenter, server, clock, samples, sizes)
        require(all(a.closed for a in adapters.values()), "Shared client pool did not close")
    overlap = sum(any(r["started"] < end and r["completed"] > begin for begin, end in collection_windows)
                  for r in read_batches)
    require(overlap > 0, "No MCP read batch overlapped collection")
    require(any(s == "ok" for s in statuses), "No successful collection task")
    if any(cycle % 4 == 1 for cycle in range(first, end)):
        require(any(s in ("partial", "error") for s in statuses), "Injected partial source was not reported")
    with store.connection() as con:
        for table in ("candles", "observations", "book_snapshots"):
            require(not con.execute(f"SELECT 1 FROM {table} WHERE data_mode!='demo' LIMIT 1").fetchone(),
                    f"Synthetic {table} persisted outside demo mode")
        require(not con.execute("SELECT 1 FROM runs WHERE json_extract(payload,'$.data_mode') IS NOT 'demo' LIMIT 1")
                .fetchone(), "Synthetic run persisted outside demo mode")
    expected_keys = set(initial_candle_keys)
    for source, adapter in adapters.items():
        for call in adapter.calls:
            if call["kind"] != "ohlcv" or not call["returned_rows"]:
                continue
            now = args.fixture_now + call["cycle"] * step
            latest = int(now) // 3600 * 3600 - 3600
            first_open = max(latest - 24 * 3600, math.ceil(call["since_ms"] / 3600000) * 3600)
            asset, quote = call["symbol"].split("/")
            expected_keys.update((source, asset, quote, first_open + i * 3600)
                                 for i in range(call["returned_rows"]))
    candles = [c for asset in settings.assets for c in store.candles(asset, mode="demo", as_of=int(clock.time()))]
    actual_keys = {(c.source, c.asset, c.quote, c.open_time) for c in candles}
    require(actual_keys == expected_keys, "Collection lost or invented native candle events")
    for candle in candles:
        expected_price = adapters[candle.source].price(f"{candle.asset}/{candle.quote}")
        require(candle.close == expected_price + 1 and candle.open == expected_price,
                "Native market financial value changed")
        require(candle.close_time <= candle.observed_at <= clock.time(), "Candle known-at chronology changed")
    (folder / "checkpoint.json").write_text(canonical({"clock": clock.now, "counts": store.counts(),
                                                      "states": durable_states(store), "pid": os.getpid()}))
    calls = [dict(call, source=source) for source, adapter in adapters.items() for call in adapter.calls]
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"phase": args.phase, "pid": os.getpid(), "cycles": end - first,
            "elapsed_seconds": time.perf_counter() - started, "restart": restart,
            "collection_latency": distribution(collect_samples), "mcp_call_latency": distribution(samples),
            "mcp_serialized_response_bytes": sizes, "frozen_history_page_calls": sum(page_calls),
            "read_batches_overlapping_collection": overlap, "collection_statuses": statuses,
            "adapter_calls": calls, "actual_http_requests": 0,
            "adapter_returned_candle_rows": sum(c.get("returned_rows", 0) for c in calls),
            "storage_samples": storage_samples, "scheduler_samples": states,
            "native_candle_oracle_rows": len(candles),
            "peak_process_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
            "lifecycle": lifecycle, "correctness_mismatches": []}


def lifecycle_checks(store, settings, presenter, server, clock, samples, sizes):
    from crypto_market_analysis.models import CollectionResult
    from crypto_market_analysis.storage_policy import (
        StoragePolicy,
        inspect_storage,
        maintain_storage,
        verify_archive,
    )

    # Every phase verifies archives produced during the same scheduler workload.
    policy = StoragePolicy.from_settings(settings)
    before = inspect_storage(store.path, policy)
    maintain_storage(store, policy, apply=False, now=int(clock.time()))
    require(before == inspect_storage(store.path, policy), "Maintenance dry run changed storage")
    maintained = maintain_storage(store, policy, apply=True, now=int(clock.time()))
    inventory = inspect_storage(store.path, policy)
    verified = [verify_archive(store.path.with_name(store.path.stem + "-archives") / row["name"])
                for row in inventory["archives"]]
    require(bool(verified), "Scheduler workload did not produce a checked archive")
    rows_before = store.counts()
    store.storage_policy = replace(policy, hot_max_bytes=1)
    try:
        store.record_run(CollectionResult("synthetic-quota"), "probe", int(clock.time()), data_mode="demo")
    except ValueError:
        pass
    else:
        require(False, "Hot admission quota accepted a write")
    finally:
        store.storage_policy = policy
    require(store.counts() == rows_before, "Rejected quota write changed stored rows")
    # Guaranteed eligible old run so archive budget failure exercises real publication.
    store.record_run(CollectionResult("synthetic-archive-probe"), "probe", int(clock.time()) - 3 * 86400)
    store.record_run(CollectionResult("synthetic-archive-probe"), "probe", int(clock.time()))
    logical_before = inspect_storage(store.path, policy)["families"]
    try:
        maintain_storage(store, replace(policy, archive_max_bytes=1), apply=True, now=int(clock.time()))
    except ValueError:
        pass
    else:
        require(False, "Archive budget accepted a new archive")
    require(inspect_storage(store.path, policy)["families"] == logical_before,
            "Archive budget failure deleted hot evidence")
    meta = presenter.present({"oracle": [1, 2, 3]}, {"tool": "synthetic-expiry"})["_mcp_result"]
    selection = {key: meta[key] for key in ("handle", "selection_hash", "content_hash")}
    asyncio.run(mcp_call(server, "get_result_page", {**selection, "content_hash": "0" * 64},
                        samples, sizes, expect_error=True))
    original_now = clock.now
    clock.now += presenter.cache.ttl + 1
    try:
        asyncio.run(mcp_call(server, "get_result_page", selection, samples, sizes, expect_error=True))
    finally:
        clock.now = original_now
    try:
        presenter.cache.put({"oversize": "x" * (presenter.cache.max_bytes + 1)}, {})
    except ValueError:
        pass
    else:
        require(False, "Frozen cache exceeded configured byte bound")
    return {"dry_run_preserved_inventory": True, "maintenance_deleted_rows": sum(
                row["deleted_rows"] for row in maintained["families"]),
            "verified_archive_files": len(verified), "verified_archive_rows": sum(len(v["rows"]) for v in verified),
            "hot_quota_write_refused": True, "archive_quota_preserved_hot_evidence": True,
            "mcp_wrong_hash_refused": True, "mcp_expired_handle_refused": True,
            "mcp_cache_byte_limit_refused": True}


def bounded_int(low, high):
    def parse(value):
        parsed = int(value)
        if not low <= parsed <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return parsed
    return parse


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--output", type=Path, help="JSON report outside the repository; default stdout")
    result.add_argument("--virtual-days", type=bounded_int(1, 30), default=7)
    result.add_argument("--cycles-per-day", type=bounded_int(1, 288), default=4,
                        help="Requested virtual samples/day; max-cycles may reduce this cadence")
    result.add_argument("--max-cycles", type=bounded_int(4, 2048), default=512)
    result.add_argument("--readers", type=bounded_int(1, 8), default=2)
    result.add_argument("--reads-per-cycle", type=bounded_int(1, 8), default=1)
    result.add_argument("--slow-ms", type=bounded_int(1, 20), default=3,
                        help="Real bounded delay per slow-source book call")
    result.add_argument("--timeout", type=bounded_int(5, 300), default=60,
                        help="Hard wall-clock seconds for both finite worker processes together")
    result.add_argument("--fixture-now", type=int, default=1780272000,
                        help="Synthetic start UTC epoch seconds; no real-provider timestamps")
    result.add_argument("--phase", type=int, choices=(1, 2), help=argparse.SUPPRESS)
    result.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    return result


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if args.output and args.output.resolve().is_relative_to(root):
        command.error("--output must be outside the repository")
    if args.fixture_now < 86400 * 4 or args.fixture_now + args.virtual_days * 86400 > 4102444800:
        command.error("--fixture-now must keep the complete fixture between 1970-01-05 and 2100-01-01")
    if args.phase:
        require(args.data_dir is not None, "Internal worker requires an explicit data directory")
        with block_network() as attempts:
            report = run_phase(args.data_dir, args)
            require(not attempts, "Scenario attempted network access")
        print(canonical(report))
        return 0
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--", "src", "tools/benchmark_endurance.py"],
                                        cwd=root, text=True).strip())
    total = min(args.virtual_days * args.cycles_per_day + 1, args.max_cycles)
    report = {"schema_version": 1, "source_commit": revision, "source_dirty": dirty,
              "python": sys.version, "synthetic": True, "network_policy": "all socket egress and DNS blocked",
              "actual_http_requests": 0, "limits": vars(args) | {"output": str(args.output) if args.output else None,
                        "data_dir": None, "effective_cycles": total,
                        "effective_virtual_step_seconds": args.virtual_days * 86400 / (total - 1)},
              "limitations": ["Accelerated sparse workload; virtual days are not real elapsed soak time.",
                              "Local single-machine observations are not a production SLO or distributed capacity proof.",
                              "Synthetic CCXT adapter calls/returned rows are not observed HTTP requests or wire bytes.",
                              "MCP uses real registered request handlers and SDK serialization, without transport framing.",
                              "Fixture-only Store subclass explicitly labels pipeline-produced evidence demo at ingestion.",
                              "Process restart is graceful, not a power loss or interrupted SQLite transaction.",
                              "RSS samples include imports/fixture setup; first two cycles per process are warmup.",
                              "SQLite budgets are admission limits, not an OS filesystem quota; no provider acceptance tested."],
              "phases": [], "correctness_mismatches": []}
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="cma-endurance-") as directory:
            for phase in (1, 2):
                remaining = args.timeout - (time.perf_counter() - started)
                require(remaining > 0, "Endurance wall-clock deadline exceeded")
                invocation = [sys.executable, str(Path(__file__).resolve()), "--phase", str(phase),
                              "--data-dir", directory]
                for name in ("virtual_days", "cycles_per_day", "max_cycles", "readers", "reads_per_cycle",
                             "slow_ms", "timeout", "fixture_now"):
                    invocation.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
                completed = subprocess.run(invocation, cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
                                           capture_output=True, text=True, timeout=remaining, check=False)
                require(completed.returncode == 0, f"Worker {phase} failed: {completed.stderr[-5000:]}")
                report["phases"].append(json.loads(completed.stdout))
        report["elapsed_seconds"] = time.perf_counter() - started
        report["collection_latency"] = distribution([v for phase in report["phases"]
                                                      for v in phase["collection_latency"]["samples_ms"]])
        report["mcp_call_latency"] = distribution([v for phase in report["phases"]
                                                   for v in phase["mcp_call_latency"]["samples_ms"]])
        calls = [call for phase in report["phases"] for call in phase["adapter_calls"]]
        warmed_rss = [sample["rss_bytes"] for phase in report["phases"] for sample in phase["storage_samples"]
                      if not sample["warmup"] and sample["rss_bytes"] is not None]
        report["observed_work"] = {"adapter_calls": len(calls),
                                   "adapter_catalogue_calls": sum(c["kind"] == "catalogue" for c in calls),
                                   "adapter_ohlcv_calls": sum(c["kind"] == "ohlcv" for c in calls),
                                   "adapter_book_calls": sum(c["kind"] == "book" for c in calls),
                                   "adapter_returned_candle_rows": sum(c.get("returned_rows", 0) for c in calls),
                                   "actual_http_requests": 0,
                                   "mcp_handler_calls": report["mcp_call_latency"]["count"],
                                   "actual_process_restarts": 1}
        report["rss_after_warmup"] = {"sample_count": len(warmed_rss),
                                      "median_bytes": statistics.median(warmed_rss) if warmed_rss else None,
                                      "maximum_bytes": max(warmed_rss) if warmed_rss else None}
        require(all(not p["correctness_mismatches"] for p in report["phases"]), "Worker reported mismatches")
        report["status"] = "passed"
    except (AssertionError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        report["status"] = "failed"
        report["correctness_mismatches"].append(str(exc))
        report["elapsed_seconds"] = time.perf_counter() - started
    output = canonical(report) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(f"{report['status']}: {args.output}", file=sys.stderr)
    else:
        print(output, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
