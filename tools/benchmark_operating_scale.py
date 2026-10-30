"""Finite offline operating-read benchmark for 100 and 1000 synthetic assets.

Each case uses an isolated subprocess and temporary store. These figures measure
this fixture and machine; they establish neither provider support nor uptime.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from crypto_market_analysis.cost_report import operating_cost_report
from crypto_market_analysis.models import Candle, CollectionResult
from crypto_market_analysis.operating_acceptance import operating_health
from crypto_market_analysis.storage import Store
from crypto_market_analysis.storage_policy import physical_storage


def measure(assets, runs):
    now = int(time.time())
    end = now // 3600 * 3600
    with tempfile.TemporaryDirectory(prefix="operating-scale-") as directory:
        path = Path(directory) / "observer.sqlite"
        store = Store(path)
        candles = [Candle("fixture", f"ASSET{i}", "USD", "1h", end-3600*(hour+1), end-3600*hour,
                          1, 1, 1, 1, 1, now, data_mode="demo") for i in range(assets) for hour in range(24)]
        build = time.perf_counter()
        store.put_candles(candles)
        accounting = {"coverage": "observed", "totals": {"requests": 1, "responses": 1,
            "errors": 0, "http_errors": 0, "decoded_body_bytes": 128, "unknown_body_responses": 0, "elapsed_ms": 2}}
        # Deliberately >10k rows: cost aggregation must not cap a global run tail.
        result = CollectionResult("fixture", http_accounting=accounting)
        payload = store.record_run(result, "market", now, data_mode="demo")
        with store.connection() as con:
            con.executemany("INSERT INTO runs(source,task,observed_at,payload) VALUES(?,?,?,?)",
                (("fixture", "market", now, json.dumps(payload)) for _ in range(runs-1)))
        build_seconds = time.perf_counter()-build
        started, cpu = time.perf_counter(), time.process_time()
        cost = operating_cost_report(path, "demo", as_of=now)
        cost_seconds = time.perf_counter()-started
        health_start = time.perf_counter()
        pages, groups, cursor = 0, 0, None
        while True:
            health = operating_health(path, "demo", as_of=now, limit=100, cursor=cursor)
            pages += 1
            groups += len(health["rows"])
            if any(row["status"] != "pass" for row in health["rows"]):
                raise RuntimeError("Synthetic complete coverage did not qualify.")
            cursor = health["pagination"]["next_cursor"]
            if cursor is None:
                break
        return {"assets": assets, "native_markets": assets, "candle_versions": len(candles),
            "retained_runs": runs, "measured_requests": cost["rows"][0]["requests"],
            "health_groups": groups, "health_pages": pages, "build_seconds": build_seconds,
            "cost_seconds": cost_seconds, "health_seconds": time.perf_counter()-health_start,
            "read_cpu_seconds": time.process_time()-cpu,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss *
                (1 if sys.platform == "darwin" else 1024),
            "physical": physical_storage(path), "response_json_bytes": len(json.dumps(cost).encode()),
            "provenance": "synthetic_demo_fixture; no network or provider qualification"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="+", type=int, default=[100, 1000])
    parser.add_argument("--runs", type=int, default=20000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.runs <= 1_000_000 or any(not 1 <= count <= 1000 for count in args.assets):
        parser.error("Use 1–1000 assets and 1–1000000 runs.")
    if args.child:
        print(json.dumps(measure(args.assets[0], args.runs)))
        return 0
    rows = []
    for count in args.assets:
        result = subprocess.run([sys.executable, __file__, "--child", "--assets", str(count),
                                 "--runs", str(args.runs)], capture_output=True, text=True,
                                timeout=300, check=True, env={**os.environ, "PYTHONHASHSEED": "0"})
        rows.append(json.loads(result.stdout))
    body = json.dumps({"schema_version": 1, "cases": rows,
        "scope": "isolated synthetic fixture on this machine; no production latency or multiweek uptime claim"}, indent=2)+"\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
