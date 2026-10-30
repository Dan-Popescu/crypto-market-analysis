"""Incremental public-data collection and restart-safe low-frequency scheduling."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import replace
from threading import Event

from .asset_universe import SPECIALIZED_ASSETS, validate_assets
from .collection_plan import CollectionProgress, collection_plan
from .collectors import ExchangeClients, collect_exchange
from .config import Settings
from .http_accounting import merge_measurements
from .http_limits import HTTPBudgetExceeded, bounded_http, check_http_budget, current_http_budget
from .models import CollectionResult
from .providers import collect_context, collect_options
from .storage import Store

log = logging.getLogger(__name__)


def _combined_exchange_reports(reports: list[dict]) -> list[dict]:
    """Keep the one-shot summary shape while storing independent task outcomes."""
    grouped = {}
    combined = []
    for report in reports:
        if report["task"] in ("market", "liquidity"):
            grouped.setdefault(report["source"], []).append(report)
        else:
            combined.append(report)
    for rows in grouped.values():
        if len(rows) == 1:
            combined.extend(rows)
            continue
        rows.sort(key=lambda row: ("market", "liquidity").index(row["task"]))
        report = {**rows[0], "task": "+".join(row["task"] for row in rows),
                  "observed_at": max(row["observed_at"] for row in rows)}
        for key in ("candles", "observations", "inserted_candles", "inserted_observations"):
            report[key] = sum(row[key] for row in rows)
        for key in ("errors", "warnings"):
            report[key] = [value for row in rows for value in row[key]]
        report["http_accounting"] = merge_measurements([row.get("http_accounting") for row in rows])
        if (budget := current_http_budget()) is not None:
            report["http_accounting"]["http_limits"] = budget.snapshot()
        has_data = report["candles"] or report["observations"]
        report["status"] = ("partial" if has_data else "error") if report["errors"] else (
            "ok" if has_data else "empty")
        combined.append(report)
    return combined


@bounded_http
def collect(
    settings: Settings,
    store: Store,
    *,
    tasks: tuple[str, ...] = ("market", "liquidity", "options", "context"),
    days: int | None = None,
    clients: ExchangeClients | None = None,
    source_tasks: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    if hasattr(settings, "storage_maintenance_enabled"):
        from .storage_policy import StoragePolicy
        store.storage_policy = StoragePolicy.from_settings(settings)
    if clients is None:
        with ExchangeClients() as owned:
            return collect(settings, store, tasks=tasks, days=days, clients=owned, source_tasks=source_tasks)
    validate_assets(settings.assets)
    planned = collection_plan(settings, tasks=tasks)
    if (len(settings.assets) > settings.collection_batch_size
            or planned["market_work_units"] + planned["liquidity_work_units"] > settings.collection_work_limit):
        return _collect_many(settings, store, tasks, days, clients, source_tasks)
    now = int(time.time())
    since = now - (days or settings.history_days) * 86400
    reports = []

    def ingest(result: CollectionResult, task: str) -> None:
        # First availability is ingestion completion, never the beginning of a slow HTTP request.
        completed = int(time.time())
        result.candles = [replace(c, observed_at=completed) for c in result.candles]
        result.observations = [replace(o, observed_at=completed) for o in result.observations]
        reports.append(store.save_result(result, task, completed, data_mode="live"))

    def exchange(venue: str, venue_tasks: tuple[str, ...]) -> CollectionResult:
        latest = store.latest_candle_times(venue, settings.assets, quotes=settings.quote_currencies)
        starts = {(asset, quote): latest.get((asset, quote))
                  for asset in settings.assets for quote in settings.quote_currencies}
        cursors = {
            market: since if days is not None or latest is None else max(0, latest - 3600)
            for market, latest in starts.items()
        }
        liquidity = "liquidity" in venue_tasks and (
            settings.liquidity_venues is None or venue in settings.liquidity_venues
        )
        include = tuple(
            [
                *(("candles", "derivatives") if "market" in venue_tasks else ()),
                *(("liquidity",) if liquidity else ()),
            ]
        )
        return collect_exchange(
            venue,
            ([asset for asset in settings.assets if settings.liquidity_assets is None
              or asset in settings.liquidity_assets] if venue_tasks == ("liquidity",) else settings.assets),
            since=since,
            include=include,
            max_pages=settings.max_pages,
            quotes=tuple(settings.quote_currencies),
            since_by_market=cursors,
            clients=clients,
        )

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        futures = {}
        if "market" in tasks or "liquidity" in tasks:
            for venue in settings.venues:
                venue_tasks = tuple(t for t in tasks if t in ("market", "liquidity")
                                    and (source_tasks is None or t in source_tasks.get(venue, ()))
                                    and (t != "liquidity" or settings.liquidity_venues is None
                                         or venue in settings.liquidity_venues))
                groups = ((task,) for task in venue_tasks)
                for selected in groups:
                    if selected:
                        futures[pool.submit(copy_context().run, exchange, venue, selected)] = (venue, "+".join(selected))
        if ("options" in tasks and settings.options
                and (source_tasks is None or "options" in source_tasks.get("deribit", ()))):
            futures[pool.submit(copy_context().run, collect_options, [asset for asset in settings.assets if asset in SPECIALIZED_ASSETS])] = ("deribit", "options")
        if "context" in tasks and settings.context:
            sources = tuple(source for source in ("fred", "coinmetrics", "defillama")
                            if source_tasks is None or "context" in source_tasks.get(source, ()))
            if sources:
                futures[pool.submit(copy_context().run, collect_context, days=min(days or 365, 3650), sources=sources)] = ("context", "context")
        for future in as_completed(futures):
            source, task = futures[future]
            try:
                value = future.result()
                for result in value if isinstance(value, list) else [value]:
                    ingest(result, task)
            except Exception as exc:
                failed_sources = sources if source == "context" else (source,)
                for failed_source in failed_sources:
                    ingest(CollectionResult(failed_source, errors=[f"Collection: {type(exc).__name__}: {exc}"]), task)
    if source_tasks is None:
        reports = _combined_exchange_reports(reports)
    return {
        "started_at": now,
        "completed_at": int(time.time()),
        "paid_collection_apis": False,
        "collection_plan": collection_plan(settings, tasks=tasks),
        "http_limits": current_http_budget().snapshot(),
        "reports": sorted(reports, key=lambda r: r["source"]),
        "status": "error"
        if reports and all(r["status"] == "error" for r in reports)
        else "partial"
        if any(r["status"] in ("error", "partial") for r in reports)
        else "ok",
    }



def _collect_many(settings, store, tasks, days, clients, source_tasks):
    """Dispatch a bounded fair rotation, persisting each pair before releasing rows.

    A source/task gets at most one native pair before another source/task runs.
    Dispatch is serial so a tiny shared HTTP allowance cannot repeatedly be won
    by the same worker. Each CCXT instance has at most one request in flight.
    """
    now = int(time.time())
    since = now - (days or settings.history_days) * 86400
    jobs = []
    units = {}
    for source in settings.venues:
        for task in ("market", "liquidity"):
            if task not in tasks or (source_tasks is not None and task not in source_tasks.get(source, ())):
                continue
            if task == "liquidity" and settings.liquidity_venues is not None and source not in settings.liquidity_venues:
                continue
            assets = settings.assets if task == "market" or settings.liquidity_assets is None else settings.liquidity_assets
            pairs = [(asset, quote) for asset in assets for quote in settings.quote_currencies]
            if pairs:
                jobs.append((source, task))
                units[(source, task)] = pairs
    specialized = [asset for asset in settings.assets if asset in SPECIALIZED_ASSETS]
    if "options" in tasks and settings.options and (source_tasks is None or "options" in source_tasks.get("deribit", ())):
        jobs.append(("deribit", "options"))
        units[("deribit", "options")] = [(asset, None) for asset in specialized] or [(None, None)]
    if "context" in tasks and settings.context:
        for source in ("fred", "coinmetrics", "defillama"):
            if source_tasks is None or "context" in source_tasks.get(source, ()):
                jobs.append((source, "context"))
                units[(source, "context")] = [(None, None)]
    progress = CollectionProgress(store)
    positions = {job: progress.position(*job, units[job]) for job in jobs}
    dispatch_universe = "source_task_dispatch"
    dispatch = progress.position("*", "dispatch", dispatch_universe)
    attempted = {job: 0 for job in jobs}
    quotas = {job: len(units[job]) - positions[job] % len(units[job]) for job in jobs}
    summaries = {}
    cursor_cache = {}
    limit = min(settings.collection_work_limit, sum(quotas.values()))
    completed = 0
    while jobs and completed < limit:
        try:
            check_http_budget()
        except HTTPBudgetExceeded:
            break
        eligible = [job for job in jobs if attempted[job] < quotas[job]]
        if not eligible:
            break
        job = eligible[dispatch % len(eligible)]
        source, task = job
        pairs = units[job]
        asset, quote = pairs[positions[job] % len(pairs)]
        # Persist the reservation before invoking catalogue/provider I/O. An
        # interrupted or failed attempt cannot pin every later asset forever.
        positions[job] += 1
        dispatch += 1
        progress.advance(*job, pairs, positions[job])
        progress.advance("*", "dispatch", dispatch_universe, dispatch)
        attempted[job] += 1
        completed += 1
        try:
            if task in ("market", "liquidity"):
                if source not in cursor_cache:
                    cursor_cache[source] = store.latest_candle_times(source, settings.assets, quotes=settings.quote_currencies)
                latest = cursor_cache[source].get((asset, quote))
                cursor = since if days is not None or latest is None else max(0, latest - 3600)
                result = collect_exchange(source, [asset], since=since,
                    include=("candles", "derivatives") if task == "market" else ("liquidity",),
                    max_pages=settings.max_pages, quotes=(quote,),
                    since_by_market={(asset, quote): cursor}, clients=clients)
                results = [result]
            elif task == "options":
                results = ([collect_options([asset])] if asset is not None else
                           [CollectionResult(source, warnings=["Options unavailable or unknown for configured assets; supported adapter assets: BTC, ETH"])])
            else:
                results = collect_context(days=min(days or 365, 3650), sources=(source,))
        except Exception as exc:
            results = [CollectionResult(source, errors=[f"Collection: {type(exc).__name__}: {exc}"])]
        for result in results:
            finished = int(time.time())
            result.candles = [replace(row, observed_at=finished) for row in result.candles]
            result.observations = [replace(row, observed_at=finished) for row in result.observations]
            if attempted[job] < quotas[job]:
                result.warnings.append("pending_collection_work")
            row = store.save_result(result, task, finished, data_mode="live")
            _accumulate_task_report(summaries, row)
    reports = list(summaries.values())
    for row in reports:
        row["warnings"] = [warning for warning in row["warnings"] if warning != "pending_collection_work"]
    pending = []
    for job in jobs:
        if attempted[job] < quotas[job]:
            pending.append({"source": job[0], "task": job[1],
                            "attempted": attempted[job], "remaining_in_rotation": quotas[job] - attempted[job],
                            "next_pair_position": positions[job] % len(units[job])})
            if job in summaries:
                summaries[job]["warnings"].append("pending_collection_work")
    if source_tasks is None:
        reports = _combined_exchange_reports(reports)
    return {"started_at": now, "completed_at": int(time.time()), "paid_collection_apis": False,
            "reports": sorted(reports, key=lambda row: (row["source"], row["task"])),
            "collection_plan": collection_plan(settings, tasks=tasks),
            "collection_progress": {"work_attempted": completed, "pending": pending,
                                    "dispatch": "serial fair native-pair rotation; durable before I/O"},
            "status": "error" if reports and all(row["status"] == "error" for row in reports)
                      else "partial" if pending or any(row["status"] in ("error", "partial") for row in reports)
                      else "ok"}


def _accumulate_task_report(summaries, row):
    key = (row["source"], row["task"])
    if key not in summaries:
        summaries[key] = row
        return
    summary = summaries[key]
    for field in ("candles", "observations", "inserted_candles", "inserted_observations"):
        summary[field] += row[field]
    for field in ("errors", "warnings"):
        summary[field].extend(row[field])
    summary["observed_at"] = max(summary["observed_at"], row["observed_at"])
    summary["http_accounting"] = merge_measurements([summary.get("http_accounting"), row.get("http_accounting")])
    has_data = summary["candles"] or summary["observations"]
    summary["status"] = ("partial" if has_data else "error") if summary["errors"] else ("ok" if has_data else "empty")


def run_scheduler(settings: Settings, store: Store, stop: Event | None = None) -> None:
    with ExchangeClients() as clients:
        _run_scheduler(settings, store, stop, clients)


def _run_scheduler(settings: Settings, store: Store, stop: Event | None, clients: ExchangeClients) -> None:
    from .retention import compact_liquidity
    from .scheduling import Schedule

    if hasattr(settings, "storage_maintenance_enabled"):
        from .storage_policy import StoragePolicy
        store.storage_policy = StoragePolicy.from_settings(settings)
    stop = stop or Event()
    intervals = {
        "market": settings.market_interval,
        "liquidity": settings.liquidity_interval,
        "options": settings.options_interval,
        "context": settings.context_interval,
        "retention": 86400,
    }
    # Persist every historical source/task clock before maintenance may archive runs.
    schedule = Schedule(store, intervals)
    jobs = {(source, "market") for source in settings.venues}
    jobs.update((source, "liquidity") for source in
                (settings.venues if settings.liquidity_venues is None else settings.liquidity_venues))
    if settings.options:
        jobs.add(("deribit", "options"))
    if settings.context:
        jobs.update((source, "context") for source in ("fred", "coinmetrics", "defillama"))
    while not stop.is_set():
        now = time.time()
        states = schedule.states()
        maintenance = states.get(("storage", "retention"), {})
        if maintenance.get("next_due", 0) <= now:
            schedule.start("storage", "retention", now)
            try:
                stats = {}
                for _ in range(7):
                    stats = compact_liquidity(store, settings.liquidity_retention_days)
                    if not stats["remaining_old_rows"]:
                        break
                pending = bool(stats.get("remaining_old_rows"))
                notices = [str(stats), *(["pending_backlog"] if pending else [])]
                result = CollectionResult("storage", warnings=notices)
                if getattr(settings, "storage_maintenance_enabled", False):
                    from .retention import maintain_storage
                    from .storage_policy import StoragePolicy
                    notices.append(str(maintain_storage(store, StoragePolicy.from_settings(settings), apply=True)))
            except Exception as exc:
                pending = False
                result = CollectionResult("storage", errors=[f"Maintenance: {type(exc).__name__}: {exc}"])
            completed = time.time()
            report = store.record_run(result, "retention", int(completed))
            schedule.record(report, "retention", completed)
        source_tasks = {}
        for source, task in sorted(jobs):
            if states.get((source, task), {}).get("next_due", 0) <= now:
                source_tasks.setdefault(source, []).append(task)
        if source_tasks:
            for source, selected in source_tasks.items():
                for task in selected:
                    schedule.start(source, task, now)
            task_selection = tuple(t for t in intervals if any(t in ts for ts in source_tasks.values()))
            failure = "Collection returned no report"
            try:
                report = collect(settings, store, tasks=task_selection, clients=clients,
                                 source_tasks={source: tuple(ts) for source, ts in source_tasks.items()})
            except Exception as exc:
                failure = f"Collection: {type(exc).__name__}: {exc}"
                report = {"status": "error", "reports": []}
            completed = time.time()
            received = set()
            pending_jobs = {(row["source"], row["task"])
                            for row in report.get("collection_progress", {}).get("pending", [])}
            for row in report.get("reports", []):
                for task in row["task"].split("+"):
                    if task in source_tasks.get(row["source"], ()):
                        schedule.record(row, task, completed)
                        received.add((row["source"], task))
            for source, selected in source_tasks.items():
                for task in selected:
                    if (source, task) not in received:
                        # A missing report must still advance a durable failure clock.
                        deferred = (source, task) in pending_jobs
                        row = store.record_run(CollectionResult(source,
                                               warnings=["pending_collection_work"] if deferred else [],
                                               errors=[] if deferred else [failure]),
                                               task, int(completed), data_mode="live")
                        schedule.record(row, task, completed)
            log.info("Collection: %s", report["status"])
        stop.wait(5)
