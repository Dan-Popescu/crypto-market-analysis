"""Finite, restartable prospective operating evidence over the real pipeline.

There is no simulated passage of time. Each invocation has finite cycle and wall
bounds; independent reports preserve completed receipts and interrupted attempts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path
from threading import Event

from . import __version__
from .collectors import ExchangeClients
from .cost_report import bounded_database_report, limit_database_read, operating_cost_report
from .http_accounting import public_error
from .http_limits import HTTPPolicy, http_budget_scope
from .operational_reads import read_page_result, read_page_state, validate_source_identifier
from .pipeline import collect
from .storage import Store, database_table_names, read_database_if_present
from .storage_policy import physical_storage


@bounded_database_report
def operating_health(database, mode="live", *, source=None, as_of=None, limit=20, cursor=None,
                     required_history_hours=24, freshness_seconds=10800, max_gap_hours=0,
                     max_query_rows=250_000, query_seconds=5):
    """Return bounded known-at hourly coverage per stored native source/market.

    Missing configured markets cannot appear here; the acceptance runner emits
    explicit unknown rows for its configured markets. No record body is loaded.
    """
    validate_source_identifier(source)
    if mode not in ("live", "import", "demo"):
        raise ValueError("Select live, import or demo mode.")
    for name, value, low, high in (("required_history_hours", required_history_hours, 1, 8760),
            ("freshness_seconds", freshness_seconds, 0, 31536000),
            ("max_gap_hours", max_gap_hours, 0, 8760), ("max_query_rows", max_query_rows, 1, 2_000_000)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}.")
    selection = {"kind": "operating_health", "mode": mode, "source": source,
                 "required_history_hours": required_history_hours, "freshness_seconds": freshness_seconds,
                 "max_gap_hours": max_gap_hours, "max_query_rows": max_query_rows}
    with read_database_if_present(database) as con:
        limit_database_read(con, query_seconds=query_seconds)
        available = "candles" in database_table_names(con)
        state = read_page_state(con if available else None, "candles", selection, as_of, limit, cursor)
        end = state["as_of"] // 3600 * 3600
        start = max(0, end - required_history_hours * 3600)
        rows = []
        if available:
            keys = con.execute("""SELECT source,asset,quote FROM candles WHERE data_mode=?
                AND observed_at<=? AND id<=? AND timeframe='1h' AND (? IS NULL OR source=?)
                GROUP BY source,asset,quote ORDER BY source,asset,quote LIMIT ? OFFSET ?""",
                (mode, state["as_of"], state["high"], source, source, limit + 1, state["offset"]))
            for key in keys:
                identity = dict(zip(("source", "asset", "quote"), key, strict=True))
                params = (*key, mode, state["as_of"], state["high"], start, end)
                clause = """source=? AND asset=? AND quote=? AND data_mode=? AND observed_at<=?
                    AND id<=? AND timeframe='1h' AND open_time>=? AND close_time<=?"""
                count = con.execute(f"SELECT COUNT(*) FROM candles WHERE {clause}", params).fetchone()[0]
                row = {**identity, "market": f"{key[1]}/{key[2]}", "stored_versions": count}
                if count > max_query_rows:
                    rows.append({**row, "status": "unknown", "reason": "query_row_budget_exceeded"})
                    continue
                summary = con.execute(f"""WITH ranked AS (SELECT open_time,close_time,observed_at,
                    ROW_NUMBER() OVER(PARTITION BY open_time ORDER BY observed_at DESC,id DESC) rn
                    FROM candles WHERE {clause}), selected AS
                    (SELECT *,LAG(close_time) OVER(ORDER BY open_time) previous_end FROM ranked WHERE rn=1)
                    SELECT COUNT(*),MIN(open_time),MAX(close_time),MAX(observed_at),
                    MAX(MAX(0,open_time-COALESCE(previous_end,?))),
                    MAX(MAX(0,observed_at-close_time)) FROM selected""", [*params, start]).fetchone()
                hours, first, last, receipt, interior, lag = summary
                largest = max(interior or 0, end - (last or start)) // 3600
                missing = (end - start) // 3600 - hours
                age = state["as_of"] - last if last is not None else None
                rows.append({**row, "status": ("pass" if not missing and age <= freshness_seconds
                    and largest <= max_gap_hours else "incomplete"),
                    "coverage": {"hours": hours, "required_hours": (end-start)//3600,
                                 "missing_hours": missing, "largest_gap_hours": largest},
                    "freshness": {"latest_closed_at": last, "age_seconds": age,
                                  "status": "unknown" if age is None else "fresh" if age <= freshness_seconds else "stale"},
                    "availability": {"latest_receipt": receipt, "max_receipt_lag_seconds": lag},
                    "first_closed_hour_open": first})
        return {**read_page_result(state, rows, limit, selection), "status": "available" if available else "database_unavailable",
                "required_range": {"start": start, "end": end},
                "query_budget": {"sqlite_seconds": query_seconds, "progress_check_vm_instructions": 10000,
                                 "max_versions_per_market": max_query_rows, "market_page_limit": 100},
                "knowledge_basis": "observed_at <= cutoff; latest received version per native hourly record",
                "missing_market_basis": "Unlisted markets are unknown, not unsupported or covered."}


def _normalize(settings, request):
    defaults = {"refresh": False, "session_id": "default", "max_cycles": 1,
                "max_duration_seconds": 60, "interval_seconds": 300, "tasks": ["market"],
                "cycle_http_requests": min(settings.http_max_requests, 1000),
                "cycle_deadline_seconds": min(settings.http_deadline_seconds, 30),
                "required_history_hours": 24, "freshness_seconds": 10800, "max_gap_hours": 0}
    if not isinstance(request, dict) or set(request) - set(defaults):
        raise ValueError("Unknown operating acceptance request fields.")
    value = {**defaults, **request}
    if type(value["refresh"]) is not bool:
        raise ValueError("refresh must be boolean.")
    if not isinstance(value["session_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value["session_id"]):
        raise ValueError("session_id must contain 1–64 safe identifier characters.")
    for key, low, high in (("max_cycles", 1, 10000), ("max_duration_seconds", 1, 86400),
                          ("interval_seconds", 1, 86400), ("cycle_http_requests", 1, 100000),
                          ("required_history_hours", 1, 8760), ("freshness_seconds", 0, 31536000),
                          ("max_gap_hours", 0, 8760)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    deadline = value["cycle_deadline_seconds"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or not 0 < deadline <= 300:
        raise ValueError("cycle_deadline_seconds must be finite and between 0 and 300.")
    if (not isinstance(value["tasks"], list) or not value["tasks"] or len(set(value["tasks"])) != len(value["tasks"])
            or set(value["tasks"]) - {"market", "liquidity", "options", "context"}):
        raise ValueError("Select distinct market, liquidity, options and/or context tasks.")
    return value


def _write(path, value):
    if path.is_symlink():
        raise ValueError("Operating artifacts must not be symbolic links.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="." + path.name + "-",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(value, sort_keys=True, allow_nan=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path):
    if path.is_symlink() or path.stat().st_size > 16 * 1024**2:
        raise ValueError("Operating report exceeds the 16 MiB read bound.")
    return json.loads(path.read_text())


@contextmanager
def _session_lock(directory):
    # OS locks release automatically after a crashed runner; no stale PID file.
    with (directory / "session.lock").open("a") as handle:
        if os.name == "nt":
            import msvcrt

            handle.write("0")
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("This operating acceptance session already has an active runner.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("This operating acceptance session already has an active runner.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _usage():
    try:
        import resource
        import sys

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {"cpu_seconds": usage.ru_utime + usage.ru_stime,
                "peak_rss_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)}
    except ImportError:
        return {"cpu_seconds": None, "peak_rss_bytes": None}


def _cycle_health(settings, request, as_of, *, deadline):
    rows, cursor = [], None
    inspection_deadline = min(time.monotonic() + 5, deadline)
    inspection_limited = False
    # Explicit upper bound: 1000 assets x 16 venues x 4 quotes, returned by page.
    for _ in range(640):
        remaining = inspection_deadline - time.monotonic()
        if remaining <= 0:
            inspection_limited = True
            break
        report = operating_health(settings.database, as_of=as_of, limit=100, cursor=cursor, query_seconds=remaining,
            **{k: request[k] for k in ("required_history_hours", "freshness_seconds", "max_gap_hours")})
        if "pagination" not in report:
            return {"as_of": as_of, "status": "unknown", "markets": [], "reason": report["status"]}
        rows.extend(report["rows"])
        cursor = report["pagination"]["next_cursor"]
        if cursor is None:
            break
        if time.monotonic() >= inspection_deadline:
            inspection_limited = True
            break
    configured = {(s, a, q) for s in settings.venues for a in settings.assets for q in settings.quote_currencies}
    found = {(r["source"], r["asset"], r["quote"]) for r in rows}
    selected = [r for r in rows if (r["source"], r["asset"], r["quote"]) in configured]
    selected.extend({"source": s, "asset": a, "quote": q, "market": f"{a}/{q}",
        "status": "unknown", "reason": "no_known_at_hourly_evidence"} for s, a, q in sorted(configured-found))
    return {"as_of": as_of, "markets": selected, "truncated": cursor is not None,
            "inspection_budget_exhausted": inspection_limited,
            "inspection_limit_basis": "Five seconds checked between bounded five-second SQLite pages; unknown rows may be uninspected.",
            "provider_support": "Coverage never establishes capability, rights or purchase eligibility."}


def _row_counts(database, *, query_seconds=5):
    """Count one optional read snapshot; interruption keeps every count unknown."""
    unknown = {table: None for table in ("candles", "observations", "runs")}
    if query_seconds <= 0:
        return unknown
    try:
        with read_database_if_present(database) as con:
            limit_database_read(con, query_seconds=min(5, query_seconds))
            tables = database_table_names(con)
            return {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if table in tables else None for table in unknown}
    except sqlite3.DatabaseError:
        return unknown


def operating_acceptance_exit_code(report):
    """Project capture outcome independently of how many cycles completed."""
    status = (report.get("last_cycle") or {}).get("status", report.get("status"))
    return int(status in {"error", "partial", "budget_exhausted", "blocked"})


def run_operating_acceptance(settings, request, *, clients=None, stop=None):
    """Run or inspect finite live cycles; restart retains durable cadence and reports.

    No historical ``as_of`` may be supplied for capture. HTTP limits are shared
    per cycle and cooperative, not forced process deadlines. The caller owns
    injected clients; their evidence cannot establish public provider acceptance.
    """
    request = _normalize(settings, request)
    directory = settings.data_dir / "operating_acceptance" / request["session_id"]
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("Operating session directories must not be symbolic links.")
    manifest_path = directory / "manifest.json"
    if not request["refresh"]:
        return {"kind": "operating_acceptance", "status": "recorded" if manifest_path.is_file() else "not_recorded",
                "read_only": True, "network_requested": False,
                "manifest": _read_json(manifest_path) if manifest_path.is_file() else None,
                "cost": operating_cost_report(settings.database),
                "health": operating_health(settings.database, **{k: request[k] for k in
                    ("required_history_hours", "freshness_seconds", "max_gap_hours")})}
    if (settings.data_dir / "dataset.json").exists():
        raise ValueError("Operating capture requires a live store, never a named import dataset.")
    if len(settings.assets) > 1000 or len(settings.venues) > 16 or len(settings.quote_currencies) > 4:
        raise ValueError("Operating capture supports at most 1000 assets, 16 venues and 4 native quotes.")
    if clients is None:
        with ExchangeClients() as owned:
            return _run(settings, request, owned, directory, manifest_path, stop or Event(), False)
    return _run(settings, request, clients, directory, manifest_path, stop or Event(), True)


def _run(settings, request, clients, directory, manifest_path, stop, injected):
    directory.mkdir(parents=True, exist_ok=True)
    identity = {k: v for k, v in request.items() if k not in ("refresh", "max_cycles", "max_duration_seconds")}
    identity.update(assets=settings.assets, venues=settings.venues, quote_currencies=settings.quote_currencies,
                    injected_clients=injected)
    settings_snapshot = asdict(settings)
    settings_snapshot["data_dir"] = str(settings.data_dir.expanduser().resolve())
    source_directory = Path(__file__).parent
    workload_modules = ("operating_acceptance", "pipeline", "collectors", "providers", "config",
                        "asset_universe", "storage", "http_limits", "http_accounting")
    identity.update(settings=settings_snapshot, software={
        "application_version": __version__, "ccxt_version": version("ccxt"),
        "httpx_version": version("httpx"), "requests_version": version("requests"),
        "source_sha256": {name: hashlib.sha256((source_directory / f"{name}.py").read_bytes()).hexdigest()
                          for name in workload_modules},
    })
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
    started = time.monotonic()
    invocation_deadline = started + request["max_duration_seconds"]
    with _session_lock(directory):
        manifest = (_read_json(manifest_path) if manifest_path.is_file() else
                    {"schema_version": 1, "session_id": request["session_id"], "first_started_at": time.time(),
                     "attempted_cycles": 0, "completed_cycles": 0, "next_due": 0,
                     "measured_active_seconds": 0, "request_sha256": fingerprint, "selection": identity})
        if manifest["request_sha256"] != fingerprint:
            raise ValueError("Session selection changed; choose a new session_id.")
        store = Store(settings.database)
        completed = 0
        last = None
        while completed < request["max_cycles"] and not stop.is_set():
            remaining = invocation_deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = max(0, manifest["next_due"] - time.time())
            if delay:
                stop.wait(min(delay, remaining, 30))
                continue
            now = time.time()
            cycle_start = time.monotonic()
            manifest["attempted_cycles"] += 1
            sequence = manifest["attempted_cycles"]
            manifest["next_due"] = now + request["interval_seconds"]
            path = directory / f"cycle-{sequence:08d}.json"
            _write(path, {"status": "interrupted_or_in_progress", "started_at": now, "sequence": sequence})
            _write(manifest_path, manifest)
            before, usage = physical_storage(settings.database), _usage()
            before_rows = _row_counts(settings.database, query_seconds=invocation_deadline-time.monotonic())
            capture_start = time.monotonic()
            remaining = invocation_deadline - time.monotonic()
            if remaining <= 0:
                collection = {"status": "budget_exhausted", "reports": [],
                              "http_limits": {"exceeded": "invocation_deadline", "used_requests": 0}}
            else:
                scoped = replace(settings, http_max_requests=request["cycle_http_requests"],
                    http_deadline_seconds=min(request["cycle_deadline_seconds"], remaining))
                try:
                    with http_budget_scope(HTTPPolicy.from_settings(scoped)):
                        collection = collect(scoped, store, tasks=tuple(request["tasks"]), clients=clients)
                except Exception as exc:
                    collection = {"status": "error", "reports": [], "errors": [public_error(exc, include_message=False)]}
            known_at = int(time.time())
            capture_elapsed = time.monotonic() - capture_start
            try:
                quality = _cycle_health(settings, request, known_at, deadline=invocation_deadline)
            except (ValueError, OSError) as exc:
                quality = {"as_of": known_at, "status": "unknown", "error": public_error(exc, include_message=False)}
            after_rows = _row_counts(settings.database, query_seconds=invocation_deadline-time.monotonic())
            elapsed = time.monotonic() - cycle_start
            after_usage = _usage()
            observed_http = [r.get("http_accounting") or {} for r in collection.get("reports", [])]
            observed_response = any((m.get("totals") or {}).get("responses", 0) > 0 for m in observed_http)
            cycle_status = ("budget_exhausted" if collection.get("http_limits", {}).get("exceeded")
                            else collection["status"])
            last = {"schema_version": 1, "kind": "operating_acceptance_cycle", "sequence": sequence,
                "started_at": now, "completed_at": time.time(), "as_of": known_at,
                "elapsed_seconds": elapsed, "capture_elapsed_seconds": capture_elapsed,
                "status": cycle_status, "collection": collection,
                "provider_acceptance": "unverified_injected_transport" if injected else
                    "observed_transport_this_cycle_only" if observed_response else "unverified_or_failed",
                "quality": quality,
                "resources": {"before": before, "after": physical_storage(settings.database),
                    "rows_before": before_rows, "rows_after": after_rows,
                    "row_growth": {k: v-before_rows[k] if v is not None and before_rows[k] is not None
                                   else None for k, v in after_rows.items()},
                    "row_count_basis": "single read-only snapshot per before/after sample; five-second ceiling and remaining invocation budget; unavailable counts remain null",
                    "cpu_seconds": (after_usage["cpu_seconds"]-usage["cpu_seconds"]
                        if usage["cpu_seconds"] is not None else None),
                    "process_peak_rss_bytes": after_usage["peak_rss_bytes"],
                    "scope": "CPU delta for cycle, process RSS lifetime peak; whole-store row/byte growth may include other writers; not per-source allocation"}}
            _write(path, last)
            completed += 1
            manifest["completed_cycles"] += 1
            manifest["measured_active_seconds"] += elapsed
            manifest["last_completed_at"] = time.time()
            manifest["elapsed_wall_seconds"] = manifest["last_completed_at"] - manifest["first_started_at"]
            manifest["last_report"] = path.name
            _write(manifest_path, manifest)
        return {"kind": "operating_acceptance", "status": "completed" if completed else "no_cycle_due",
                "network_requested": completed > 0, "completed_this_invocation": completed,
                "invocation_elapsed_seconds": time.monotonic()-started,
                "max_duration_seconds": request["max_duration_seconds"],
                "manifest": manifest, "last_cycle": last,
                "report_directory": str(directory),
                "duration_basis": "actual UTC wall span includes downtime; measured active seconds exclude downtime",
                "qualification": "prospective_evidence_only; no multiweek acceptance before actual elapsed observation",
                "limits_basis": "finite cycles; remaining monotonic invocation budget caps each HTTP/SQLite scope; cooperative transport and VM deadlines, not process termination"}
