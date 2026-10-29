"""Incremental public-data collection and restart-safe low-frequency scheduling."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from threading import Event

from .collectors import ExchangeClients, collect_exchange
from .config import Settings
from .models import CollectionResult
from .providers import collect_context, collect_options
from .storage import Store

log = logging.getLogger(__name__)


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
        starts = {
            (asset, quote): store.latest_candle_time(venue, asset, quote=quote)
            for asset in settings.assets
            for quote in settings.quote_currencies
        }
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
            settings.assets,
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
                groups = ((task,) for task in venue_tasks) if source_tasks is not None else (venue_tasks,)
                for selected in groups:
                    if selected:
                        futures[pool.submit(exchange, venue, selected)] = (venue, "+".join(selected))
        if ("options" in tasks and settings.options
                and (source_tasks is None or "options" in source_tasks.get("deribit", ()))):
            futures[pool.submit(collect_options, settings.assets)] = ("deribit", "options")
        if "context" in tasks and settings.context:
            sources = tuple(source for source in ("fred", "coinmetrics", "defillama")
                            if source_tasks is None or "context" in source_tasks.get(source, ()))
            if sources:
                futures[pool.submit(collect_context, days=min(days or 365, 3650), sources=sources)] = ("context", "context")
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
    return {
        "started_at": now,
        "completed_at": int(time.time()),
        "paid_collection_apis": False,
        "reports": sorted(reports, key=lambda r: r["source"]),
        "status": "error"
        if reports and all(r["status"] == "error" for r in reports)
        else "partial"
        if any(r["status"] in ("error", "partial") for r in reports)
        else "ok",
    }


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
            for row in report.get("reports", []):
                for task in row["task"].split("+"):
                    if task in source_tasks.get(row["source"], ()):
                        schedule.record(row, task, completed)
                        received.add((row["source"], task))
            for source, selected in source_tasks.items():
                for task in selected:
                    if (source, task) not in received:
                        # A missing report must still advance a durable failure clock.
                        row = store.record_run(CollectionResult(source, errors=[failure]),
                                               task, int(completed), data_mode="live")
                        schedule.record(row, task, completed)
            log.info("Collection: %s", report["status"])
        stop.wait(5)
