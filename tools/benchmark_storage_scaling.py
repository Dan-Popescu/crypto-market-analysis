"""Measure real SQLite and DuckDB engines on bounded synthetic candle vintages.

Synthetic symbols bypass collectors and market admission to measure storage.
This does not establish venue support, HTTP capacity or production throughput. Each
asset scale runs in a fresh subprocess; Parquet retains every received version.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

START = 1_700_002_800


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(rows):
    return hashlib.sha256(canonical(rows).encode()).hexdigest()


def timed(function, repetitions):
    samples = []
    result = None
    for _ in range(repetitions):
        started = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - started) * 1000)
    return result, {"samples_ms": samples, "median_ms": statistics.median(samples), "max_ms": max(samples)}


def populate(path, assets, hours):
    from crypto_market_analysis.models import Candle
    from crypto_market_analysis.storage import Store

    store = Store(path)
    with store.connection() as con:
        batch = []
        for asset_number in range(assets):
            asset = f"SYNTHETIC{asset_number:04d}"
            for source in ("synthetic-a", "synthetic-b"):
                for quote in ("EUR", "USD"):
                    for hour in range(hours):
                        opened = START + hour * 3600
                        for revision in range(2):
                            candle = Candle(source, asset, quote, "1h", opened, opened + 3600,
                                            100., 103., 99., 101. + revision, 10.,
                                            opened + 3600 + revision * 7200, data_mode="demo")
                            payload = canonical(asdict(candle))
                            batch.append((source, asset, quote, "1h", opened, opened + 3600,
                                          candle.observed_at, "demo", hashlib.sha256(payload.encode()).hexdigest(), payload))
                        if len(batch) >= 4096:
                            con.executemany("INSERT INTO candles(source,asset,quote,timeframe,open_time,close_time,observed_at,data_mode,fingerprint,payload) VALUES(?,?,?,?,?,?,?,?,?,?)", batch)
                            batch = []
        con.executemany("INSERT INTO candles(source,asset,quote,timeframe,open_time,close_time,observed_at,data_mode,fingerprint,payload) VALUES(?,?,?,?,?,?,?,?,?,?)", batch)
    return store


def postgres_measure(args, store, root, selected_query, selected_parameters, fleet_query, fleet_parameters):
    """Use only an explicitly supplied disposable local container, never provision one.

    psql/Docker round trips and server EXPLAIN execution times are reported
    separately. The caller owns this container's creation and cleanup.
    """
    command = ["env", "-u", "DOCKER_HOST", "-u", "DOCKER_CONTEXT", "-u", "DOCKER_TLS", "-u", "DOCKER_TLS_VERIFY", "-u", "DOCKER_CERT_PATH",
               "docker", "--host=unix:///var/run/docker.sock", "exec", "-i", args.postgres_container,
               "psql", "-XqAt", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-c"]

    def run(sql, stdin=None):
        result = subprocess.run([*command, sql], stdin=stdin, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"Isolated PostgreSQL benchmark failed: {result.stderr[:1000]}")
        return result.stdout.strip()

    def literal(value):
        return str(value) if type(value) is int else "'" + value.replace("'", "''") + "'"

    def bind(query, parameters):
        pieces = query.split("?")
        if len(pieces) != len(parameters) + 1:
            raise ValueError("Internal PostgreSQL parameter mismatch")
        return "".join(part + (literal(parameters[index]) if index < len(parameters) else "") for index, part in enumerate(pieces))

    started = time.perf_counter()
    run("DROP TABLE IF EXISTS candles; CREATE TABLE candles(id BIGINT PRIMARY KEY,source TEXT,asset TEXT,quote TEXT,timeframe TEXT,open_time BIGINT,close_time BIGINT,observed_at BIGINT,data_mode TEXT,fingerprint TEXT,payload TEXT)")
    csv_path = root / "postgres.csv"
    with store.connection() as con, csv_path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows(con.execute("SELECT id,source,asset,quote,timeframe,open_time,close_time,observed_at,data_mode,fingerprint,payload FROM candles ORDER BY id"))
    with csv_path.open() as stream:
        run("COPY candles FROM STDIN WITH (FORMAT csv)", stream)
    run("CREATE INDEX candle_lookup ON candles(source,asset,quote,timeframe,open_time,data_mode,observed_at DESC,id DESC); ANALYZE candles")
    ingest_ms = (time.perf_counter() - started) * 1000
    selected_sql = bind(selected_query, selected_parameters)
    fleet_sql = bind(fleet_query, fleet_parameters)
    selected_rows, selected_roundtrip = timed(lambda: [json.loads(line) for line in run(selected_sql).splitlines()], args.repetitions)
    fleet_rows, fleet_roundtrip = timed(lambda: [tuple(int(field) if index >= 3 else field for index, field in enumerate(line.split("|")))
                                                for line in run(fleet_sql).splitlines()], args.repetitions)

    def server_times(sql):
        return [json.loads(run("EXPLAIN (ANALYZE,FORMAT JSON) " + sql))[0]["Execution Time"] for _ in range(args.repetitions)]

    selected_server = server_times(selected_sql)
    fleet_server = server_times(fleet_sql)
    return {"version": run("SELECT version()"), "load_and_index_ms": ingest_ms,
            "table_and_index_bytes": int(run("SELECT pg_total_relation_size('candles')")),
            "selected_client_roundtrip": selected_roundtrip, "fleet_client_roundtrip": fleet_roundtrip,
            "selected_server_execution_ms": selected_server, "fleet_server_execution_ms": fleet_server,
            "selected_result_sha256": digest(selected_rows), "fleet_result_sha256": digest(fleet_rows),
            "measurement": "Disposable local container, no network, 2 CPU/512 MiB when provisioned as documented; psql/Docker process overhead is included only in client round trips. Server timings exclude Python payload decoding."}


def worker(args):
    import duckdb
    import pyarrow as pa

    from crypto_market_analysis.analytical_store import (
        AnalyticalCandleStore,
        export_candle_snapshot,
        open_analytical_snapshot,
    )

    with tempfile.TemporaryDirectory(prefix="observer-storage-") as temporary:
        root = Path(temporary)
        database = root / "market.sqlite"
        started = time.perf_counter()
        store = populate(database, args.worker_assets, args.hours)
        ingest_ms = (time.perf_counter() - started) * 1000
        snapshot = root / "snapshot"
        started = time.perf_counter()
        manifest = export_candle_snapshot(store, snapshot, rows_per_file=args.rows_per_file)
        parquet_paths = [snapshot / part["file"] for part in manifest.get("files", [{"file": manifest.get("file")}])]
        row_count = manifest["rows"]
        export_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        reader = AnalyticalCandleStore(snapshot)
        open_verify_ms = (time.perf_counter() - started) * 1000
        selected = [f"SYNTHETIC{number:04d}" for number in range(min(args.worker_assets, 10))]
        placeholders = ",".join("?" for _ in selected)
        cutoff = START + args.hours * 3600
        predicates = f"asset IN ({placeholders}) AND data_mode=? AND source=? AND quote=? AND open_time>=? AND open_time<? AND close_time<=? AND observed_at<=?"
        parameters = [*selected, "demo", "synthetic-a", "EUR", cutoff - 24 * 3600, cutoff, cutoff, cutoff]
        query = """SELECT payload FROM (SELECT payload,source,asset,quote,timeframe,open_time,data_mode,
            ROW_NUMBER() OVER(PARTITION BY source,asset,quote,timeframe,open_time,data_mode
            ORDER BY observed_at DESC,id DESC) rn FROM {table} WHERE """ + predicates + ") WHERE rn=1 ORDER BY asset,source,quote,timeframe,open_time,data_mode"
        with sqlite3.connect(database) as sqlite, duckdb.connect(config={"threads": "2", "memory_limit": "256MB", "temp_directory": ""}) as analytical:
            analytical.read_parquet([str(path) for path in parquet_paths]).create_view("candles")

            def read_sqlite():
                return [json.loads(row[0]) for row in sqlite.execute(query.format(table="candles"), parameters)]

            def read_duckdb():
                return [json.loads(row[0]) for row in analytical.execute(query.format(table="candles"), parameters).fetchall()]

            sqlite_rows, sqlite_ms = timed(read_sqlite, args.repetitions)
            analytical_rows, duckdb_ms = timed(read_duckdb, args.repetitions)
            if sqlite_rows != analytical_rows:
                raise ValueError("Known-at/native-dimension engine results differ")
            expected = len(selected) * min(24, args.hours)
            if len(sqlite_rows) != expected or sqlite_rows[-1]["close"] != 101.:
                raise ValueError("Fixture failed to preserve receipt chronology")
            fleet_query = """SELECT asset,source,quote,COUNT(*),SUM(close_time-open_time) FROM
                (SELECT *,ROW_NUMBER() OVER(PARTITION BY source,asset,quote,timeframe,open_time,data_mode
                ORDER BY observed_at DESC,id DESC) rn FROM candles WHERE data_mode=? AND close_time<=? AND observed_at<=?)
                WHERE rn=1 GROUP BY asset,source,quote ORDER BY asset,source,quote"""
            fleet_parameters = ["demo", cutoff, cutoff]
            sqlite_fleet, sqlite_fleet_ms = timed(lambda: sqlite.execute(fleet_query, fleet_parameters).fetchall(), args.repetitions)
            duckdb_fleet, duckdb_fleet_ms = timed(lambda: analytical.execute(fleet_query, fleet_parameters).fetchall(), args.repetitions)
            if sqlite_fleet != duckdb_fleet:
                raise ValueError("Fleet aggregate engine results differ")
            fleet, api_fleet_ms = timed(lambda: reader.fleet_summary(
                [f"SYNTHETIC{number:04d}" for number in range(args.worker_assets)],
                start=START, end=cutoff, as_of=cutoff, mode="demo"), args.repetitions)
            fleet_comparable = [(row["asset"], row["source"], row["quote"], row["points"], row["points"] * 3600)
                                for row in fleet["series"]]
            if fleet_comparable != sqlite_fleet:
                raise ValueError("Public fleet summary and SQLite baseline differ")
            fleet_assets = [f"SYNTHETIC{number:04d}" for number in range(args.worker_assets)]

            def cached_fleet():
                return open_analytical_snapshot(snapshot).fleet_summary(
                    fleet_assets, start=START, end=cutoff, as_of=cutoff, mode="demo")

            _, cold_end_to_end_ms = timed(cached_fleet, 1)
            _, warm_end_to_end_ms = timed(cached_fleet, args.repetitions)
            api_rows, api_selected_ms = timed(lambda: reader.candles(
                selected, start=cutoff - 24 * 3600, end=cutoff, as_of=cutoff,
                source="synthetic-a", quote="EUR", mode="demo"), args.repetitions)
            if [asdict(row) for row in api_rows] != sqlite_rows:
                raise ValueError("Public candle API and SQLite baseline differ")
        postgres = None
        if args.postgres_container:
            postgres = postgres_measure(args, store, root, query.format(table="candles"), parameters, fleet_query, fleet_parameters)
            if postgres["selected_result_sha256"] != digest(sqlite_rows) or postgres["fleet_result_sha256"] != digest(sqlite_fleet):
                raise ValueError("PostgreSQL result digests differ from SQLite/DuckDB")
        return {"assets": args.worker_assets, "hours": args.hours, "vintage_rows": row_count,
                "ingest_sqlite_ms": ingest_ms, "export_all_vintages_ms": export_ms,
                "snapshot_open_verify_ms": open_verify_ms,
                "sqlite_bytes": database.stat().st_size, "parquet_bytes": sum(path.stat().st_size for path in parquet_paths),
                "snapshot_format": manifest["format"], "partition_count": len(parquet_paths),
                "selected_assets": len(selected), "selected_rows": len(sqlite_rows),
                "selected_result_sha256": digest(sqlite_rows), "fleet_result_sha256": digest(sqlite_fleet),
                "sqlite_selected": sqlite_ms, "duckdb_selected": duckdb_ms,
                "sqlite_fleet": sqlite_fleet_ms, "duckdb_fleet": duckdb_fleet_ms,
                "duckdb_api_fleet": api_fleet_ms, "duckdb_api_selected": api_selected_ms,
                "duckdb_cold_verified_fleet": cold_end_to_end_ms,
                "duckdb_warm_cached_verified_fleet": warm_end_to_end_ms,
                "postgresql": postgres,
                "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "engines": {"sqlite": sqlite3.sqlite_version, "duckdb": duckdb.__version__, "pyarrow": pa.__version__}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="+", type=int, default=[10, 100, 1000])
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows-per-file", type=int, help="Use v2 snapshots with at most this many rows per Parquet file (1..1000000)")
    parser.add_argument("--postgres-container", help="Existing disposable local observer-storage-bench-* container with psql; caller owns cleanup")
    parser.add_argument("--worker-assets", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rows_per_file is not None and not 1 <= args.rows_per_file <= 1_000_000:
        parser.error("rows-per-file must be between 1 and 1000000")
    if args.postgres_container and not re.fullmatch(r"observer-storage-bench-[a-zA-Z0-9_.-]+", args.postgres_container):
        parser.error("PostgreSQL comparison requires an explicit observer-storage-bench-* disposable container")
    if not 1 <= args.hours <= 744 or not 1 <= args.repetitions <= 10:
        parser.error("hours must be 1..744 and repetitions 1..10")
    scales = [args.worker_assets] if args.worker_assets is not None else args.assets
    if any(scale not in (10, 100, 1000) or scale * args.hours * 8 > 2_000_000 for scale in scales):
        parser.error("asset scales must be 10, 100 or 1000 and each fixture must stay under two million vintages")
    if args.worker_assets is not None:
        print(canonical(worker(args)))
        return
    results = []
    for scale in args.assets:
        command = [sys.executable, __file__, "--worker-assets", str(scale), "--hours", str(args.hours), "--repetitions", str(args.repetitions)]
        if args.postgres_container:
            command.extend(["--postgres-container", args.postgres_container])
        if args.rows_per_file is not None:
            command.extend(["--rows-per-file", str(args.rows_per_file)])
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError(f"Storage benchmark failed for {scale} assets: {result.stderr}")
        results.append(json.loads(result.stdout))
    try:
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=5)
        postgres = {"status": "not_benchmarked", "docker_available": probe.returncode == 0,
                    "reason": "PostgreSQL requires a separately provisioned database and comparable fixture; no service was created." if probe.returncode == 0 else probe.stderr.strip()[:1000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        postgres = {"status": "unavailable", "reason": str(exc)}
    report = {"schema": "observer-storage-scaling-v1", "synthetic": True,
              "scope": "Real local engines; synthetic storage capacity with collectors and market admission bypassed. No venue-support, HTTP, concurrency, cold-cache or production throughput claim.",
              "measurement": "Fresh subprocess per scale; engine order SQLite then DuckDB; timings include decoding; peak RSS covers both engines and export.",
              "postgresql": {"status": "measured", "container": args.postgres_container} if args.postgres_container else postgres,
              "results": results}
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
