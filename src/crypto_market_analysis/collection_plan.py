"""Inspectable capacity and durable fair progress for explicit asset universes."""

from __future__ import annotations

import hashlib
import json

from .asset_universe import SPECIALIZED_ASSETS, validate_assets


def collection_plan(settings, *, tasks=("market", "liquidity", "options", "context")) -> dict:
    assets = validate_assets(settings.assets)
    pairs = len(assets) * len(settings.quote_currencies)
    books = assets if settings.liquidity_assets is None else settings.liquidity_assets
    book_venues = settings.venues if settings.liquidity_venues is None else settings.liquidity_venues
    market = pairs * len(settings.venues) if "market" in tasks else 0
    liquidity = len(books) * len(settings.quote_currencies) * len(book_venues) if "liquidity" in tasks else 0
    specialized = [asset for asset in assets if asset in SPECIALIZED_ASSETS]
    return {
        "assets": len(assets), "maximum_assets": 1000, "native_quotes": list(settings.quote_currencies),
        "requested_pairs_per_venue": pairs, "venues": len(settings.venues),
        "market_work_units": market, "liquidity_work_units": liquidity,
        "liquidity_assets": len(books), "liquidity_venues": len(book_venues),
        "collection_batch_size": settings.collection_batch_size,
        "collection_work_limit": settings.collection_work_limit,
        "cadences_seconds": {task: getattr(settings, f"{task}_interval")
                             for task in ("market", "liquidity", "options", "context")},
        "logical_calls_upper_estimate_per_rotation": {
            "candles": market * settings.max_pages, "derivatives": market * 3, "books": liquidity,
            "options": len(specialized) * 2 if settings.options and "options" in tasks else 0,
        },
        "actual_http_requests": None,
        "estimate_basis": "Requested native pairs; catalogue support unknown. Logical calls exclude catalogue loads, transport retries and context provider requests; actual HTTP is measured at transport.",
        "specialized_options_assets": specialized,
        "specialized_options_unavailable_or_unknown": [asset for asset in assets if asset not in SPECIALIZED_ASSETS],
        "asset_identity": "Configured CCXT base code; ticker collisions and economic token identity are not resolved.",
    }


class CollectionProgress:
    """Reserve only dispatched work; restart and failures rotate past that attempt.

    One row per source/task plus a shared dispatch cursor keeps storage bounded.
    A changed explicit universe resets its pair cursor rather than reusing an
    index with a different meaning. No observation history is scanned.
    """

    def __init__(self, store):
        self.store = store
        self._signatures = {}
        with store.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS collection_progress (
                source TEXT NOT NULL, task TEXT NOT NULL, signature TEXT NOT NULL,
                position INTEGER NOT NULL, PRIMARY KEY(source,task))""")

    def _signature(self, universe):
        key = id(universe)
        if key not in self._signatures:
            self._signatures[key] = (universe, hashlib.sha256(json.dumps(universe, separators=(",", ":")).encode()).hexdigest())
        return self._signatures[key][1]

    def position(self, source: str, task: str, universe) -> int:
        signature = self._signature(universe)
        with self.store.connection() as con:
            row = con.execute("SELECT signature,position FROM collection_progress WHERE source=? AND task=?",
                              (source, task)).fetchone()
        return row["position"] if row and row["signature"] == signature else 0

    def advance(self, source: str, task: str, universe, position: int) -> None:
        signature = self._signature(universe)
        with self.store.connection() as con:
            con.execute("""INSERT INTO collection_progress VALUES(?,?,?,?)
                ON CONFLICT(source,task) DO UPDATE SET signature=excluded.signature,position=excluded.position""",
                        (source, task, signature, position))
