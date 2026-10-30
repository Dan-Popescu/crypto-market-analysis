"""Read-only, paginated operating costs from retained collection measurements.

SQL reduces scalar counters before returning a bounded source/operation page.
Decoded body bytes are not wire traffic; declared rates produce estimates only.
"""

from __future__ import annotations

import math
import sqlite3
import time
from functools import wraps

from .operational_reads import _page, _result, _source
from .reporting import bounded_as_of
from .storage import database_table_names, read_database_if_present


def _bounded_read_report(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except sqlite3.DatabaseError:
            return {"status": "database_unavailable_or_query_budget_exceeded", "rows": [],
                    "read_only": True, "measurement_coverage": "unknown",
                    "reason": "No result was returned from the bounded read transaction."}
    return bounded


def _limit_read(con):
    """Bound aggregate scans even when retention has left a very large store."""
    deadline = time.monotonic() + 5
    if con is not None:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)


def _prices(prices):
    allowed = {"currency", "per_million_requests", "per_gib_decoded_bytes"}
    if prices is None:
        return None
    if not isinstance(prices, dict) or set(prices) - allowed:
        raise ValueError("prices supports currency, per_million_requests and per_gib_decoded_bytes.")
    if not isinstance(prices.get("currency"), str) or not 1 <= len(prices["currency"]) <= 12:
        raise ValueError("Declare a bounded price currency.")
    for key, value in prices.items():
        if key != "currency" and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("Declared unit prices must be finite and nonnegative.")
    return dict(prices)


@_bounded_read_report
def operating_cost_report(database, mode="live", *, source=None, task=None, start=None,
                          as_of=None, limit=20, cursor=None, prices=None):
    """Aggregate all matching retained runs, returning at most 100 groups per page.

    ``as_of`` is a receipt cutoff. A cursor freezes run insertion high-water and
    cutoff; retention may still remove rows. Missing stores remain missing.
    CPU, RSS, resource prices and actual invoices are unavailable in run payloads.
    """
    _source(source)
    _source(task)
    if mode not in ("live", "demo", "import", "unknown", "all"):
        raise ValueError("Select live, demo, import, unknown or all mode.")
    if start is not None:
        start = bounded_as_of(start)
    prices = _prices(prices)
    selection = {"kind": "operating_cost", "mode": mode, "source": source, "task": task,
                 "start": start, "prices": prices}
    with read_database_if_present(database) as con:
        _limit_read(con)
        available = "runs" in database_table_names(con)
        state = _page(con if available else None, "runs", selection, as_of, limit, cursor)
        if start is not None and start > state["as_of"]:
            raise ValueError("start must be <= as_of.")
        rows = []
        if available:
            where = ["id<=?", "observed_at<=?"]
            params = [state["high"], state["as_of"]]
            for key, value in (("source", source), ("task", task)):
                if value is not None:
                    where.append(f"{key}=?")
                    params.append(value)
            if start is not None:
                where.append("observed_at>=?")
                params.append(start)
            if mode == "unknown":
                where.append("json_extract(payload,'$.data_mode') IS NULL")
            elif mode != "all":
                where.append("json_extract(payload,'$.data_mode')=?")
                params.append(mode)
            fields = ("requests", "responses", "errors", "http_errors", "decoded_body_bytes",
                      "unknown_body_responses", "elapsed_ms")
            counters = ",".join(f"SUM(json_extract(payload,'$.http_accounting.totals.{key}')) AS {key}"
                                for key in fields)
            sql = f"""SELECT source,task,COUNT(*) retained_runs,MIN(observed_at) first_receipt,
                MAX(observed_at) last_receipt,
                SUM(json_extract(payload,'$.http_accounting.totals.requests') IS NOT NULL) measured_runs,
                SUM(COALESCE(json_extract(payload,'$.http_accounting.coverage')='partial',0)) partial_runs,
                SUM(COALESCE(json_array_length(payload,'$.errors'),0)) collection_errors,
                SUM(COALESCE(json_extract(payload,'$.candles'),0)) returned_candles,
                SUM(COALESCE(json_extract(payload,'$.observations'),0)) returned_observations,
                {counters} FROM runs WHERE {' AND '.join(where)}
                GROUP BY source,task ORDER BY source,task LIMIT ? OFFSET ?"""
            rows = [dict(r) for r in con.execute(sql, [*params, limit + 1, state["offset"]])]
            for row in rows:
                row["operation"] = row.pop("task")
                row["unmeasured_runs"] = row["retained_runs"] - row["measured_runs"]
                row["measurement_coverage"] = ("unknown" if not row["measured_runs"] else
                    "partial" if row["unmeasured_runs"] or row["partial_runs"] else "observed")
                row["receipt_span_seconds"] = row["last_receipt"] - row["first_receipt"]
                row["mean_attempt_latency_ms"] = (row["elapsed_ms"] / row["requests"]
                                                   if row["requests"] and row["elapsed_ms"] is not None else None)
                estimate = None
                if prices:
                    estimate = {"currency": prices["currency"], "basis": "declared_rates_observed_subset_only",
                                "request_cost": None, "decoded_body_cost": None}
                    if "per_million_requests" in prices and row["requests"] is not None:
                        estimate["request_cost"] = row["requests"] / 1_000_000 * prices["per_million_requests"]
                    if "per_gib_decoded_bytes" in prices and row["decoded_body_bytes"] is not None:
                        estimate["decoded_body_cost"] = row["decoded_body_bytes"] / 1024**3 * prices["per_gib_decoded_bytes"]
                row["estimated_network_cost"] = estimate
        return {**_result(state, rows, limit, selection),
                "status": "available" if available else "database_unavailable",
                "generated_at": int(time.time()), "actual_bill": None, "estimated_resource_cost": None,
                "cpu_seconds": None, "peak_rss_bytes": None,
                "query_budget": {"sqlite_seconds": 5, "progress_check_vm_instructions": 10000,
                                 "group_page_limit": 100},
                "measurement_basis": "retained source/task transport attempts including retries; decoded response bytes",
                "limitations": ["Receipt span is not continuous uptime or elapsed collection CPU time.",
                    "Wire, request, TLS bytes and uninstrumented attempts remain unknown.",
                    "Run rows count returned records; revisions/overlap mean these are not net inserted rows.",
                    "Retention and archives limit this evidence; no lifetime or invoice claim."]}
