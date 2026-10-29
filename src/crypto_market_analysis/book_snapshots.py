"""Opt-in bounded public book evidence and native-market analysis bridges.

Received rows retain their JSON values, ordering and additional CCXT level fields.
Decimal analysis inputs are derived separately; CCXT's prior float conversion
cannot be reversed. A limited REST response never establishes whole-book coverage.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collectors import ExchangeClients
    from .storage import Store

MAX_LEVELS = 500
_MAX_BYTES = 1_000_000
_MAX_TIME = 253402300799


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _clock(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_TIME:
        raise ValueError(f"{name} requires nonnegative integer UTC epoch seconds.")
    return value


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
        raise ValueError(f"{name} requires a printable nonempty label of at most 256 characters.")
    return value


def _selectors(sources: list[str], asset: str, quote: str, data_mode: str, as_of: int) -> None:
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32 or len(set(sources)) != len(sources):
        raise ValueError("sources requires 1 to 32 unique source identifiers.")
    for source in sources:
        _label(source, "source")
    if asset not in ("BTC", "ETH") or quote not in ("EUR", "USD", "USDT", "USDC"):
        raise ValueError("Select BTC/ETH and an explicit native EUR/USD/USDT/USDC quote.")
    if data_mode not in ("live", "import", "demo"):
        raise ValueError("Select an explicit live, import or demo mode.")
    _clock(as_of, "as_of")


def retained_book(
    book: dict, *, source: str, asset: str, quote: str, symbol: str,
    captured_at: int, requested_limit: int, data_mode: str = "live",
    provenance: dict | None = None,
) -> dict:
    """Copy at most 500 received rows per side without inventing an event clock.

    ``timestamp`` follows CCXT's epoch-millisecond contract. Missing timestamps
    remain null, including when receipt time is known. Invalid clocks reject the
    capture; future clocks remain evidence but are ineligible before that time.
    """
    _selectors([source], asset, quote, data_mode, captured_at)
    _label(symbol, "symbol")
    if type(requested_limit) is not int or not 1 <= requested_limit <= MAX_LEVELS:
        raise ValueError("requested_limit must be from 1 to 500.")
    if not isinstance(book, dict):
        raise ValueError("A CCXT book must be an object.")
    timestamp = book.get("timestamp")
    if timestamp is not None:
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or timestamp != int(timestamp) or not 0 <= timestamp <= _MAX_TIME * 1000):
            raise ValueError("Exchange timestamp must be nonnegative epoch milliseconds or null.")
        timestamp = int(timestamp)
    row = {
        "source": source, "asset": asset, "quote": quote, "symbol": symbol,
        "market_type": "spot", "data_mode": data_mode,
        "event_time": timestamp // 1000 if timestamp is not None else None,
        "exchange_timestamp_ms": timestamp, "captured_at": captured_at,
        "requested_limit": requested_limit,
        "provenance": {**deepcopy(provenance or {}), "timestamp_source": "exchange" if timestamp is not None else "unknown"},
        "coverage": {"whole_book_complete": False, "completeness": "unknown"},
    }
    for side in ("bids", "asks"):
        levels = book.get(side, [])
        if not isinstance(levels, list):
            raise ValueError(f"{side} must contain received rows.")
        row[side] = deepcopy(levels[:MAX_LEVELS])
        row["coverage"].update({f"returned_{side}_levels": len(levels),
                                f"retained_{side}_levels": len(row[side]),
                                f"{side}_truncated": len(levels) > MAX_LEVELS})
    encoded = _json(row)
    if len(encoded.encode()) > _MAX_BYTES:
        raise ValueError("Book evidence exceeds the one-megabyte bound.")
    row["content_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    row["input_reference"] = f"book-sha256:{row['content_hash']}"
    return row


def _validated(row: dict) -> dict:
    """Validate serialized evidence again at the persistence boundary."""
    if not isinstance(row, dict):
        raise ValueError("Book evidence requires an object.")
    raw = {"bids": row.get("bids"), "asks": row.get("asks"), "timestamp": row.get("exchange_timestamp_ms")}
    rebuilt = retained_book(
        raw, source=row.get("source"), asset=row.get("asset"), quote=row.get("quote"),
        symbol=row.get("symbol"), captured_at=row.get("captured_at"),
        requested_limit=row.get("requested_limit"), data_mode=row.get("data_mode"),
        provenance=row.get("provenance"),
    )
    # Keep the original received/truncation counts, which cannot be reconstructed
    # from the bounded retained rows alone.
    coverage = row.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("whole_book_complete") is not False or coverage.get("completeness") != "unknown":
        raise ValueError("Limited responses cannot assert whole-book completeness.")
    for side in ("bids", "asks"):
        received, kept = coverage.get(f"returned_{side}_levels"), coverage.get(f"retained_{side}_levels")
        if (type(received) is not int or type(kept) is not int or received < kept
                or kept != len(row[side]) or kept > MAX_LEVELS
                or coverage.get(f"{side}_truncated") is not (received > MAX_LEVELS)):
            raise ValueError("Inconsistent retained-book coverage.")
    rebuilt["coverage"] = deepcopy(coverage)
    encoded = _json({key: value for key, value in rebuilt.items() if key not in ("content_hash", "input_reference")})
    rebuilt["content_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    rebuilt["input_reference"] = f"book-sha256:{rebuilt['content_hash']}"
    if rebuilt != row or len(_json(rebuilt).encode()) > _MAX_BYTES:
        raise ValueError("Book evidence content or hash is inconsistent.")
    return rebuilt


def retain_books(store: Store, snapshots: list[dict]) -> int:
    """Persist exact bounded captures idempotently in a module-owned local table."""
    if not isinstance(snapshots, list) or len(snapshots) > 256:
        raise ValueError("A write supports at most 256 captured books.")
    rows = [_validated(row) for row in snapshots]
    inserted = 0
    with store.connection() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS book_snapshots (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, asset TEXT NOT NULL,
            quote TEXT NOT NULL, data_mode TEXT NOT NULL, captured_at INTEGER NOT NULL,
            event_time INTEGER, content_hash TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)""")
        con.execute("""CREATE INDEX IF NOT EXISTS book_snapshot_lookup ON book_snapshots
            (source,asset,quote,data_mode,captured_at DESC,id DESC)""")
        for row in rows:
            inserted += con.execute("""INSERT OR IGNORE INTO book_snapshots
                (source,asset,quote,data_mode,captured_at,event_time,content_hash,payload)
                VALUES(?,?,?,?,?,?,?,?)""", (
                row["source"], row["asset"], row["quote"], row["data_mode"], row["captured_at"],
                row["event_time"], row["content_hash"], _json(row),
            )).rowcount
    return inserted


def read_book_snapshot(
    store: Store, *, sources: list[str], asset: str, quote: str, data_mode: str, as_of: int,
) -> dict:
    """Select the latest eligible native capture per source at one SQLite snapshot.

    Unknown event clocks remain visible to readers; analysis constructors exclude
    them explicitly. Future receipts and known future market events are excluded.
    No source, quote or data-mode fallback occurs.
    """
    _selectors(sources, asset, quote, data_mode, as_of)
    with store.connection() as con:
        con.execute("BEGIN")
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='book_snapshots'").fetchone() is None:
            return {"books": [], "watermark": {"max_eligible_id": None, "as_of": as_of}}
        slots = ",".join("?" for _ in sources)
        rows = con.execute(f"""SELECT payload FROM (SELECT payload,source,
            ROW_NUMBER() OVER(PARTITION BY source ORDER BY captured_at DESC,id DESC) rn
            FROM book_snapshots WHERE source IN ({slots}) AND asset=? AND quote=? AND data_mode=?
            AND captured_at<=? AND (event_time IS NULL OR event_time<=?)) WHERE rn=1""",
            (*sources, asset, quote, data_mode, as_of, as_of)).fetchall()
        watermark = con.execute(f"""SELECT MAX(id) FROM book_snapshots
            WHERE source IN ({slots}) AND asset=? AND quote=? AND data_mode=?
            AND captured_at<=? AND (event_time IS NULL OR event_time<=?)""",
            (*sources, asset, quote, data_mode, as_of, as_of)).fetchone()[0]
    selected = {row["source"]: row for row in (_validated(json.loads(raw[0])) for raw in rows)}
    return {"books": [selected[source] for source in sources if source in selected],
            "watermark": {"max_eligible_id": watermark, "as_of": as_of}}


def read_books(
    store: Store, *, sources: list[str], asset: str, quote: str, data_mode: str, as_of: int,
) -> list[dict]:
    """Read bounded latest eligible books; see read_book_snapshot for its watermark."""
    return read_book_snapshot(store, sources=sources, asset=asset, quote=quote,
                              data_mode=data_mode, as_of=as_of)["books"]


def _levels(row: dict, side: str) -> list[dict]:
    """Derive exact decimal text from the retained representation, fail closed."""
    parsed = []
    for level in row[side]:
        if not isinstance(level, list) or len(level) < 2:
            raise ValueError("Invalid received price/quantity row.")
        values = []
        for value in level[:2]:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("Invalid received decimal value.")
            try:
                number = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError("Invalid received decimal value.") from exc
            if not number.is_finite() or number <= 0:
                raise ValueError("Received levels require finite positive price and quantity.")
            rendered = format(number, "f")
            if len(rendered.split(".")[0]) > 18 or ("." in rendered and len(rendered.split(".")[1]) > 18):
                raise ValueError("Received decimal exceeds venue-analysis precision bounds.")
            values.append(rendered)
        parsed.append({"price": values[0], "quantity": values[1]})
    parsed.sort(key=lambda item: Decimal(item["price"]), reverse=side == "bids")
    if len({Decimal(item["price"]) for item in parsed}) != len(parsed):
        raise ValueError("Duplicate received prices are ambiguous.")
    return parsed


def _request(
    store: Store, *, sources: list[str], asset: str, quote: str, data_mode: str, as_of: int,
    max_age_seconds: int, max_skew_seconds: int,
) -> tuple[dict, list[dict]]:
    if quote not in ("EUR", "USD"):
        raise ValueError("Venue diagnostics support separate native EUR or USD markets only.")
    for value in (max_age_seconds, max_skew_seconds):
        if type(value) is not int or not 0 <= value <= 86400:
            raise ValueError("Freshness/skew bounds must be integers from 0 to 86400.")
    snapshot = read_book_snapshot(store, sources=sources, asset=asset, quote=quote, data_mode=data_mode, as_of=as_of)
    books = snapshot["books"]
    request = {
        "asset": asset, "quote": quote, "data_mode": data_mode, "sources": sources.copy(),
        "max_age_seconds": max_age_seconds, "max_skew_seconds": max_skew_seconds,
        "snapshots": [], "book_selection": {"as_of": as_of, "watermark": snapshot["watermark"], "selected": [], "excluded": []},
    }
    usable = []
    for row in books:
        request["book_selection"]["selected"].append({key: row[key] for key in (
            "source", "symbol", "event_time", "captured_at", "input_reference", "content_hash", "coverage", "provenance")})
        if row["event_time"] is None:
            request["book_selection"]["excluded"].append({"source": row["source"], "reason": "unknown_exchange_event_time"})
        else:
            usable.append(row)
    return request, usable


def purchase_cost_request(
    store: Store, *, sources: list[str], asset: str, quote: str, data_mode: str, as_of: int,
    budget: str, assumptions_by_source: dict | None = None, product_by_source: dict | None = None,
    purchase_eligibility_by_source: dict | None = None,
    max_age_seconds: int = 300, max_skew_seconds: int = 30,
) -> dict:
    """Construct analyzer input; fee/product/rule evidence is caller-supplied.

    Missing assumptions and products remain unknown and suppress modeled costs.
    Invalid received rows are recorded as exclusions rather than repaired.
    """
    request, books = _request(store, sources=sources, asset=asset, quote=quote, data_mode=data_mode,
                              as_of=as_of, max_age_seconds=max_age_seconds, max_skew_seconds=max_skew_seconds)
    request["budget"] = budget
    count = 0
    for row in books:
        try:
            asks, bids = _levels(row, "asks"), _levels(row, "bids")
            if not bids or not asks or Decimal(bids[0]["price"]) >= Decimal(asks[0]["price"]):
                raise ValueError("Missing or crossed received book.")
        except ValueError as exc:
            request["book_selection"]["excluded"].append({"source": row["source"], "reason": str(exc)})
            continue
        count += len(asks)
        if count > 2000:
            raise ValueError("A purchase panel supports at most 2000 retained ask levels.")
        request["snapshots"].append({
            **{key: row[key] for key in ("source", "asset", "quote", "data_mode", "event_time", "captured_at", "input_reference")},
            "asks": asks, "asks_complete": False,
            "assumptions": deepcopy((assumptions_by_source or {}).get(row["source"])),
            "product": (product_by_source or {}).get(row["source"]),
            "purchase_eligibility": deepcopy((purchase_eligibility_by_source or {}).get(row["source"])),
        })
    return request


def disagreement_request(
    store: Store, *, sources: list[str], asset: str, quote: str, data_mode: str, as_of: int,
    price_basis: str = "mid", outlier_threshold_bps: str = "100",
    max_age_seconds: int = 300, max_skew_seconds: int = 30,
) -> dict:
    """Construct same-native-quote mid/best-ask inputs, without guessing last trade."""
    if price_basis not in ("mid", "best_ask"):
        raise ValueError("Retained books support mid or best_ask, not last-trade prices.")
    request, books = _request(store, sources=sources, asset=asset, quote=quote, data_mode=data_mode,
                              as_of=as_of, max_age_seconds=max_age_seconds, max_skew_seconds=max_skew_seconds)
    request.update({"price_basis": price_basis, "outlier_threshold_bps": outlier_threshold_bps})
    for row in books:
        try:
            bids, asks = _levels(row, "bids"), _levels(row, "asks")
            if not bids or not asks or Decimal(bids[0]["price"]) >= Decimal(asks[0]["price"]):
                raise ValueError("Missing or crossed received book.")
        except ValueError as exc:
            request["book_selection"]["excluded"].append({"source": row["source"], "reason": str(exc)})
            continue
        with localcontext(Context(prec=96)):
            price = (Decimal(bids[0]["price"]) + Decimal(asks[0]["price"])) / 2
        if price_basis == "mid" and price.as_tuple().exponent < -18:
            request["book_selection"]["excluded"].append({
                "source": row["source"], "reason": "Midpoint exceeds venue-analysis decimal precision bounds."})
            continue
        request["snapshots"].append({
            **{key: row[key] for key in ("source", "asset", "quote", "data_mode", "event_time", "captured_at", "input_reference")},
            "best_bid": bids[0]["price"], "best_ask": asks[0]["price"],
            "price": format(price, "f") if price_basis == "mid" else asks[0]["price"],
        })
    return request


def capture_books(
    store: Store, *, sources: list[str], assets: list[str], quotes: tuple[str, ...],
    now: int | None = None, clients: ExchangeClients | None = None,
) -> dict:
    """One-shot bounded public capture; caller-owned reusable clients stay open.

    Derived liquidity observations from the same response are also persisted.
    This does not qualify source access or reuse permission. No keys or private
    API calls are loaded. Each venue failure is isolated from all other captures.
    """
    from .collectors import ExchangeClients, collect_exchange

    if (not isinstance(assets, list) or not assets or len(assets) > 2
            or len(set(assets)) != len(assets) or any(a not in ("BTC", "ETH") for a in assets)):
        raise ValueError("assets requires unique BTC/ETH selectors.")
    if not isinstance(quotes, tuple) or not quotes or len(set(quotes)) != len(quotes):
        raise ValueError("quotes requires explicit unique native quote selectors.")
    for quote in quotes:
        _selectors(sources, assets[0], quote, "live", now if now is not None else 0)
    if clients is None:
        with ExchangeClients() as owned:
            return capture_books(store, sources=sources, assets=assets, quotes=quotes, now=now, clients=owned)
    reports, inserted, observations = [], 0, 0
    for source in sources:
        try:
            result = collect_exchange(source, assets, now=now, include=("liquidity",), quotes=quotes,
                                      clients=clients, retain_books=True)
            written = retain_books(store, result.books) if result.books else 0
            inserted += written
            receipt = (max(row["captured_at"] for row in result.books) if result.books
                       else now if now is not None else int(time.time()))
            saved = store.save_result(result, "book_capture", receipt)
            observations += saved["inserted_observations"]
            reports.append({"source": source, "captured_books": len(result.books), "inserted_books": written,
                            "inserted_observations": saved["inserted_observations"],
                            "warnings": result.warnings, "errors": result.errors})
        except Exception as exc:
            reports.append({"source": source, "captured_books": 0, "inserted_books": 0, "inserted_observations": 0,
                            "warnings": [], "errors": [f"{type(exc).__name__}: {exc}"]})
    return {"inserted_books": inserted, "inserted_observations": observations, "reports": reports, "data_mode": "live",
            "whole_book_completeness": "unknown", "source_access_qualified": False}
