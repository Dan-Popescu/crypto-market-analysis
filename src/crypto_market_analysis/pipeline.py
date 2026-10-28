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
) -> dict:
    if clients is None:
        with ExchangeClients() as owned:
            return collect(settings, store, tasks=tasks, days=days, clients=owned)
    now = int(time.time())
    since = now - (days or settings.history_days) * 86400
    reports = []

    def ingest(result: CollectionResult, task: str) -> None:
        # First availability is ingestion completion, never the beginning of a slow HTTP request.
        completed = int(time.time())
        result.candles = [replace(c, observed_at=completed) for c in result.candles]
        result.observations = [replace(o, observed_at=completed) for o in result.observations]
        reports.append(store.save_result(result, task, completed))

    def exchange(venue: str) -> CollectionResult:
        starts = {
            (asset, quote): store.latest_candle_time(venue, asset, quote=quote)
            for asset in settings.assets
            for quote in settings.quote_currencies
        }
        cursors = {
            market: since if days is not None or latest is None else max(since, latest - 3600)
            for market, latest in starts.items()
        }
        liquidity = "liquidity" in tasks and (
            settings.liquidity_venues is None or venue in settings.liquidity_venues
        )
        include = tuple(
            [
                *(("candles", "derivatives") if "market" in tasks else ()),
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
            task = "+".join(t for t in ("market", "liquidity") if t in tasks)
            for venue in settings.venues:
                if (
                    "market" not in tasks
                    and settings.liquidity_venues is not None
                    and venue not in settings.liquidity_venues
                ):
                    continue
                venue_task = (
                    task
                    if settings.liquidity_venues is None or venue in settings.liquidity_venues
                    else "market"
                )
                futures[pool.submit(exchange, venue)] = (venue, venue_task)
        if "options" in tasks and settings.options:
            futures[pool.submit(collect_options, settings.assets)] = ("deribit", "options")
        if "context" in tasks and settings.context:
            futures[pool.submit(collect_context, days=min(days or 365, 3650))] = ("context", "context")
        for future in as_completed(futures):
            source, task = futures[future]
            try:
                value = future.result()
                for result in value if isinstance(value, list) else [value]:
                    ingest(result, task)
            except Exception as exc:
                ingest(CollectionResult(source, errors=[f"Collection: {type(exc).__name__}: {exc}"]), task)
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

    stop = stop or Event()
    # Resume cadences from persisted run timestamps; keep independent task clocks.
    previous = store.runs(limit=200)
    last_maintenance = max((r["observed_at"] for r in previous if r["task"] == "retention"), default=0)
    pending_maintenance = any(
        r["task"] == "retention" and any("pending_backlog" in w for w in r["warnings"]) for r in previous
    )
    intervals = {
        "market": settings.market_interval,
        "liquidity": settings.liquidity_interval,
        "options": settings.options_interval,
        "context": settings.context_interval,
    }
    due = {
        task: min(
            (
                max(
                    (
                        r["observed_at"]
                        for r in previous
                        if r["source"] == source and task in r["task"].split("+")
                    ),
                    default=0,
                )
                for source in (
                    (
                        settings.liquidity_venues
                        if task == "liquidity" and settings.liquidity_venues is not None
                        else settings.venues
                    )
                    if task in ("market", "liquidity")
                    else ["deribit"]
                    if task == "options"
                    else ["fred", "coinmetrics", "defillama"]
                )
            ),
            default=0,
        )
        + interval
        for task, interval in intervals.items()
    }
    while not stop.is_set():
        now = time.time()
        if pending_maintenance or now - last_maintenance >= 86400:
            stats = {}
            for _ in range(7):
                stats = compact_liquidity(store, settings.liquidity_retention_days)
                if not stats["remaining_old_rows"]:
                    break
            pending_maintenance = bool(stats.get("remaining_old_rows"))
            notices = [str(stats), *(["pending_backlog"] if pending_maintenance else [])]
            store.record_run(CollectionResult("storage", warnings=notices), "retention", int(time.time()))
            last_maintenance = now
        tasks = tuple(
            t
            for t in intervals
            if due[t] <= now and (t not in ("options", "context") or getattr(settings, t))
        )
        if tasks:
            report = collect(settings, store, tasks=tasks, clients=clients)
            for task in tasks:
                due[task] = time.time() + intervals[task]
            log.info("Collection: %s", report["status"])
        stop.wait(5)
