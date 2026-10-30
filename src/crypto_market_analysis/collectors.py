"""Capability-aware collection of public market data; timestamps are epoch seconds."""

from __future__ import annotations

import logging
import math
import re
import time
from threading import RLock
from typing import TYPE_CHECKING, Any

from .http_accounting import account_exchange, instrument_exchange, public_error
from .http_limits import HTTPBudgetExceeded, bounded_public_operation, check_http_budget
from .models import Candle, CollectionResult, Observation
from .venues import VENUES

if TYPE_CHECKING:
    from .storage import Store

QUOTES = ("USD", "USDT", "USDC", "EUR")
log = logging.getLogger(__name__)


def select_market(
    markets: dict[str, dict], asset: str, kind: str = "spot", quotes: tuple[str, ...] = QUOTES
) -> dict | None:
    """Choose one active native quote market, without conflating spot and swaps."""
    candidates = [
        m
        for m in markets.values()
        if m.get("base") == asset
        and m.get("quote") in quotes
        and m.get(kind) is True
        and m.get("active") is not False
        and (kind != "swap" or m.get("linear") is not False)
    ]
    return min(candidates, key=lambda m: (quotes.index(m["quote"]), m.get("symbol", "")), default=None)


def parse_candles(
    rows: list,
    source: str,
    asset: str,
    quote: str,
    now: int,
    since: int | None = None,
    *,
    binance_raw: bool = False,
) -> list[Candle]:
    result = {}
    for row in rows:
        try:
            t = int(row[0]) // 1000
            if t + 3600 > now or (since is not None and t < since):
                continue
            o, h, low, close, volume = map(float, row[1:6])
            if (
                not all(math.isfinite(v) for v in (o, h, low, close, volume))
                or min(o, h, low, close) <= 0
                or volume < 0
                or h < max(o, low, close)
                or low > min(o, h, close)
            ):
                continue
            qv = float(row[7]) if binance_raw else None
            taker = float(row[10]) if binance_raw else None
            if qv is not None and (not math.isfinite(qv) or qv < 0):
                qv = None
            if taker is not None and (not math.isfinite(taker) or taker < 0):
                taker = None
            result[t] = Candle(
                source, asset, quote, "1h", t, t + 3600, o, h, low, close, volume, now, qv, taker
            )
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    return [result[t] for t in sorted(result)]


def summarize_book(book: dict, source: str, asset: str, quote: str, now: int) -> list[Observation]:
    """Summarize native-quote depth; coverage only when the returned book crosses the band."""

    def levels(side):
        valid = []
        for row in book.get(side, []):
            try:
                p, q = float(row[0]), float(row[1])
                if math.isfinite(p) and math.isfinite(q) and p > 0 and q > 0:
                    valid.append((p, q))
            except (ValueError, TypeError, IndexError):
                continue
        return sorted(valid, reverse=side == "bids")

    bids, asks = levels("bids"), levels("asks")
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return []
    mid = (bids[0][0] + asks[0][0]) / 2
    ts = int(now * 1000 if book.get("timestamp") is None else book["timestamp"]) // 1000
    if ts > now:
        return []
    meta = {
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "mid": mid,
        "quote": quote,
        "timestamp_source": "exchange" if book.get("timestamp") is not None else "observation",
    }
    out = [
        Observation(
            source, asset, "spread_bps", ts, now, (asks[0][0] - bids[0][0]) / mid * 10000, "bps", quote, meta
        )
    ]
    for band in (0.005, 0.01, 0.02):
        bid_depth = sum(p * q for p, q in bids if p >= mid * (1 - band))
        ask_depth = sum(p * q for p, q in asks if p <= mid * (1 + band))
        covered_bid = bids[-1][0] <= mid * (1 - band)
        covered_ask = asks[-1][0] >= mid * (1 + band)
        label = str(band * 100).rstrip("0").rstrip(".")
        detail = {
            **meta,
            "band_percent": band * 100,
            "bid_covered": covered_bid,
            "ask_covered": covered_ask,
            "coverage_complete": covered_bid and covered_ask,
        }
        for metric, val, unit in (
            (f"depth_bid_{label}pct", bid_depth, quote),
            (f"depth_ask_{label}pct", ask_depth, quote),
        ):
            out.append(Observation(source, asset, metric, ts, now, val, unit, quote, detail))
        if bid_depth + ask_depth:
            out.append(
                Observation(
                    source,
                    asset,
                    f"depth_imbalance_{label}pct",
                    ts,
                    now,
                    (bid_depth - ask_depth) / (bid_depth + ask_depth),
                    "ratio",
                    quote,
                    detail,
                )
            )
    return out


def funding_interval_hours(data: dict) -> float | None:
    """Only convert a published interval with an explicit unit."""
    interval = data.get("interval")
    if not isinstance(interval, str):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hHsS])\s*", interval)
    if not match:
        return None
    value = float(match.group(1)) / (3600 if match.group(2).lower() == "s" else 1)
    return value if math.isfinite(value) and value > 0 else None


def _cap(client: Any, name: str) -> bool:
    return bool((getattr(client, "has", {}) or {}).get(name))


class ExchangeClients:
    """Bounded public clients with per-instance rate limits and a TTL catalogue cache.

    The owner closes the pool. A scheduler can reuse it across collection cycles;
    one-shot callers get an automatically closed pool. No account keys are loaded.
    """

    def __init__(self, catalogue_ttl: int = 3600):
        if catalogue_ttl <= 0:
            raise ValueError("catalogue_ttl must be positive")
        self.catalogue_ttl = catalogue_ttl
        self._clients: dict[str, Any] = {}
        self._catalogues: dict[str, tuple[float, dict]] = {}
        self._market_indexes: dict[str, dict[str, dict]] = {}
        self._lock = RLock()
        self._venue_locks = {venue: RLock() for venue in VENUES}

    def client(self, venue: str) -> Any:
        with self._lock:
            if venue not in VENUES:
                raise ValueError(f"Unknown venue: {venue}")
            if venue not in self._clients:
                import ccxt

                self._clients[venue] = getattr(ccxt, venue)(
                    {"enableRateLimit": True, "timeout": 10000, "requests_trust_env": True}
                )
            return self._clients[venue]

    def markets(self, venue: str, client: Any) -> dict:
        cached = self._catalogues.get(venue)
        if cached is not None and time.monotonic() - cached[0] < self.catalogue_ttl:
            return cached[1]
        # CCXT caches load_markets too; refresh that catalogue when our TTL expires.
        markets = client.load_markets(reload=True) if cached else client.load_markets()
        if markets:
            self._catalogues[venue] = (time.monotonic(), markets)
            indexed = {}
            for key, market in markets.items():
                indexed.setdefault(market.get("base"), {})[key] = market
            self._market_indexes[venue] = indexed
        return markets

    def markets_for_assets(self, venue: str, assets: list[str], markets: dict) -> dict:
        """Project cached catalogue groups without rescanning every listed market."""
        indexed = self._market_indexes.get(venue)
        if indexed is None:
            return markets
        return {key: market for asset in assets
                for key, market in indexed.get(asset, {}).items()}

    def close(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, {}
            self._catalogues.clear()
            self._market_indexes.clear()
        for client in clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.exception("Failed to close public exchange client")

    def __enter__(self) -> ExchangeClients:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


@account_exchange
def collect_exchange(
    venue: str,
    assets: list[str],
    since: int | None = None,
    now: int | None = None,
    include: tuple[str, ...] = ("candles", "derivatives", "liquidity"),
    max_pages: int = 20,
    *,
    client: Any = None,
    quotes: tuple[str, ...] | None = None,
    since_by_market: dict[tuple[str, str], int | None] | None = None,
    clients: ExchangeClients | None = None,
    retain_books: bool = False,
) -> CollectionResult:
    """Collect explicit native quotes, or one preferred quote for legacy callers.

    Pagination is bounded by max_pages per asset/quote, including retries on a later
    run. Missing requested pairs remain missing; no FX or stablecoin parity applies.
    Injected clients remain owned by the caller. retain_books explicitly opts
    into bounded raw-level evidence; its single fetch also supplies liquidity.
    """
    if venue not in VENUES:
        return CollectionResult(venue, errors=[f"Unknown venue: {venue}"])
    if quotes is not None and (not quotes or any(q not in QUOTES for q in quotes)):
        raise ValueError("quotes must contain native EUR, USD, USDT or USDC quotes")
    try:
        check_http_budget()
    except HTTPBudgetExceeded as exc:
        return CollectionResult(venue, warnings=[public_error(exc)])
    if client is not None:
        instrument_exchange(client, venue)
        return _collect_exchange(
            venue, assets, since, now, include, max_pages, client, quotes, since_by_market,
            retain_books=retain_books,
        )
    if clients is None:
        with ExchangeClients() as owned:
            return collect_exchange(
                venue,
                assets,
                since,
                now,
                include,
                max_pages,
                quotes=quotes,
                since_by_market=since_by_market,
                clients=owned,
                retain_books=retain_books,
            )
    # Serialise uses of the same instance so its own CCXT rate limiter stays coherent.
    with clients._venue_locks[venue]:
        try:
            client = clients.client(venue)
            instrument_exchange(client, venue)
            markets = clients.markets(venue, client)
            project = getattr(clients, "markets_for_assets", None)
            if markets and callable(project):
                # Pool wrappers may expose only client/markets/venue locks.
                # Catalogue projection is optional on that existing protocol.
                # A nonempty catalogue with no requested base is an unsupported
                # pair, not an outage; retain a sentinel for the empty projection.
                markets = project(venue, assets, markets) or {"_unsupported": {}}
        except Exception as exc:
            return CollectionResult(venue, errors=[f"{venue} client/catalogue: {public_error(exc)}"])
        return _collect_exchange(
            venue, assets, since, now, include, max_pages, client, quotes, since_by_market, markets,
            retain_books=retain_books,
        )


def _collect_exchange(
    venue: str,
    assets: list[str],
    since: int | None,
    now: int | None,
    include: tuple[str, ...],
    max_pages: int,
    client: Any,
    quotes: tuple[str, ...] | None,
    since_by_market: dict[tuple[str, str], int | None] | None,
    markets: dict | None = None,
    *,
    retain_books: bool = False,
) -> CollectionResult:
    live_clock = now is None
    now = int(time.time()) if live_clock else int(now)
    result = CollectionResult(venue)
    try:
        markets = client.load_markets() if markets is None else markets
    except Exception as exc:
        result.errors.append(f"{venue} load_markets: {public_error(exc)}")
        return result
    if not markets:
        result.errors.append(f"{venue} load_markets: empty market catalogue; source may be unavailable")
        return result
    requests = (
        (asset, quote)
        for asset in dict.fromkeys(a.upper() for a in assets)
        for quote in (dict.fromkeys(quotes) if quotes is not None else (None,))
    )
    for asset, requested_quote in requests:
        try:
            check_http_budget()
        except HTTPBudgetExceeded as exc:
            result.warnings.append(public_error(exc))
            break
        preferred = (requested_quote,) if requested_quote else VENUES[venue].preferred_quotes
        market_since = (since_by_market or {}).get((asset, requested_quote), since)
        label = f"{asset}/{requested_quote}" if requested_quote else asset
        spot = select_market(markets, asset, quotes=preferred)
        swap = select_market(markets, asset, "swap", quotes=preferred)
        if not spot and (retain_books or any(task in include for task in ("candles", "liquidity"))):
            result.warnings.append(f"{label}: no active supported spot market")
        if spot and "candles" in include:
            if not _cap(client, "fetchOHLCV"):
                result.warnings.append(
                    f"{label}: fetchOHLCV adapter unsupported; provider availability unknown"
                )
            else:
                cursor = market_since * 1000 if market_since is not None else None
                pages = 0
                candle_count = len(result.candles)
                try:
                    while pages < max(0, min(max_pages, 100)):
                        raw_binance = (
                            venue == "binance"
                            and callable(getattr(client, "public_get_klines", None))
                            and bool(spot.get("id"))
                        )
                        if raw_binance:
                            params = {"symbol": spot["id"], "interval": "1h", "limit": 500}
                            if cursor is not None:
                                params["startTime"] = cursor
                            rows = client.public_get_klines(params)
                        else:
                            rows = client.fetch_ohlcv(spot["symbol"], "1h", since=cursor, limit=500)
                        if live_clock:
                            now = int(time.time())
                        pages += 1
                        parsed = parse_candles(
                            rows, venue, asset, spot["quote"], now, market_since, binance_raw=raw_binance
                        )
                        result.candles.extend(parsed)
                        if (
                            pages == 1
                            and parsed
                            and market_since is not None
                            and parsed[0].open_time > market_since + 3600
                        ):
                            result.warnings.append(
                                f"{label}: requested history starts at {market_since}; oldest returned closed candle "
                                f"is {parsed[0].open_time}; earlier coverage unavailable or contains gaps"
                            )
                        if not rows or market_since is None:
                            break
                        next_cursor = max(int(r[0]) for r in rows if r and r[0] is not None) + 3600000
                        if (cursor is not None and next_cursor <= cursor) or next_cursor >= now * 1000:
                            break
                        cursor = next_cursor
                    else:
                        result.warnings.append(
                            f"{label}: candle pagination limit reached; history may be incomplete"
                        )
                    if len(result.candles) == candle_count:
                        result.warnings.append(f"{label}: no closed candles returned for requested history")
                except Exception as exc:
                    result.errors.append(f"{label} fetchOHLCV: {public_error(exc)}")
                    if isinstance(exc, HTTPBudgetExceeded):
                        return result
        if spot and ("liquidity" in include or retain_books):
            if not _cap(client, "fetchOrderBook"):
                result.warnings.append(
                    f"{label}: fetchOrderBook adapter unsupported; provider availability unknown"
                )
            else:
                try:
                    book = client.fetch_order_book(spot["symbol"], limit=VENUES[venue].book_limit)
                    if live_clock:
                        now = int(time.time())
                    if "liquidity" in include:
                        obs = summarize_book(book, venue, asset, spot["quote"], now)
                        result.observations.extend(obs)
                        if not obs:
                            result.warnings.append(f"{label}: empty, crossed or future-dated order book")
                    if retain_books:
                        from .book_snapshots import retained_book

                        result.books.append(retained_book(
                            book, source=venue, asset=asset, quote=spot["quote"],
                            symbol=spot["symbol"], captured_at=now,
                            requested_limit=VENUES[venue].book_limit,
                            provenance={"adapter": "ccxt", "capability": client.has.get("fetchOrderBook"),
                                        "market_id": spot.get("id")},
                        ))
                except Exception as exc:
                    result.errors.append(f"{label} fetchOrderBook: {public_error(exc)}")
                    if isinstance(exc, HTTPBudgetExceeded):
                        return result
        if "derivatives" in include:
            if not swap:
                result.warnings.append(
                    f"{label}: no active supported swap market in adapter catalogue; provider availability unknown"
                )
                continue
            symbol, quote = swap["symbol"], swap["quote"]
            metadata = {
                "symbol": symbol,
                "market_type": "swap",
                "settle": swap.get("settle"),
                "contract_size": swap.get("contractSize"),
            }
            for capability, method, metric, value_key, unit in (
                ("fetchFundingRate", "fetch_funding_rate", "funding_rate_projected", "fundingRate", "rate"),
                (
                    "fetchOpenInterest",
                    "fetch_open_interest",
                    "open_interest_amount",
                    "openInterestAmount",
                    "unknown",
                ),
            ):
                if not _cap(client, capability):
                    result.warnings.append(
                        f"{label}: {capability} adapter unsupported; provider availability unknown"
                    )
                    continue
                try:
                    data = getattr(client, method)(symbol)
                    if live_clock:
                        now = int(time.time())
                    ts = int(now * 1000 if data.get("timestamp") is None else data["timestamp"]) // 1000
                    if ts > now:
                        result.warnings.append(f"{label} {capability}: future timestamp ignored")
                        continue
                    detail = {
                        **metadata,
                        "timestamp_source": "exchange"
                        if data.get("timestamp") is not None
                        else "observation",
                        "adapter_capability": client.has.get(capability),
                    }
                    if capability == "fetchFundingRate":
                        detail["funding_interval"] = data.get("interval")
                        detail["funding_interval_hours"] = funding_interval_hours(data)
                        hours = detail["funding_interval_hours"]
                        detail["funding_interval_seconds"] = hours * 3600 if hours is not None else None
                    value = data.get(value_key)
                    if value is not None and math.isfinite(float(value)):
                        result.observations.append(
                            Observation(venue, asset, metric, ts, now, float(value), unit, quote, detail)
                        )
                    oi_value = data.get("openInterestValue")
                    if (
                        metric == "open_interest_amount"
                        and oi_value is not None
                        and math.isfinite(float(oi_value))
                    ):
                        result.observations.append(
                            Observation(
                                venue,
                                asset,
                                "open_interest_value",
                                ts,
                                now,
                                float(oi_value),
                                "unknown",
                                quote,
                                {**detail, "value_currency": "unknown"},
                            )
                        )
                except Exception as exc:
                    result.errors.append(f"{label} {capability}: {public_error(exc)}")
                    if isinstance(exc, HTTPBudgetExceeded):
                        return result
            if _cap(client, "fetchFundingRateHistory"):
                try:
                    history = client.fetch_funding_rate_history(
                        symbol, since=market_since * 1000 if market_since is not None else None, limit=100
                    )
                    if live_clock:
                        now = int(time.time())
                    if len(history) >= 100:
                        result.warnings.append(f"{label}: funding history limited to 100 records")
                    for entry in history:
                        ts = int(entry["timestamp"]) // 1000
                        if (
                            ts <= now
                            and (market_since is None or ts >= market_since)
                            and entry.get("fundingRate") is not None
                            and math.isfinite(float(entry["fundingRate"]))
                        ):
                            result.observations.append(
                                Observation(
                                    venue,
                                    asset,
                                    "funding_rate_actual",
                                    ts,
                                    now,
                                    float(entry["fundingRate"]),
                                    "rate",
                                    quote,
                                    {
                                        **metadata,
                                        "funding_interval": entry.get("interval"),
                                        "funding_interval_hours": funding_interval_hours(entry),
                                        "funding_interval_seconds": (
                                            funding_interval_hours(entry) * 3600
                                            if funding_interval_hours(entry) is not None
                                            else None
                                        ),
                                    },
                                )
                            )
                except Exception as exc:
                    result.errors.append(f"{label} fetchFundingRateHistory: {public_error(exc)}")
                    if isinstance(exc, HTTPBudgetExceeded):
                        return result
            else:
                result.warnings.append(
                    f"{label}: fetchFundingRateHistory adapter unsupported; provider availability unknown"
                )
    result.candles = list({(c.asset, c.quote, c.open_time): c for c in result.candles}.values())
    return result


@bounded_public_operation
def capture_books(
    store: Store, *, sources: list[str], assets: list[str], quotes: tuple[str, ...],
    now: int | None = None, clients: ExchangeClients | None = None,
) -> dict:
    """One-shot bounded public capture; caller-owned reusable clients stay open.

    Derived liquidity observations from the same response are also persisted.
    This does not qualify source access or reuse permission. No keys or private
    API calls are loaded. Each venue failure is isolated from all other captures.
    """
    from .book_snapshots import retain_books

    if (not isinstance(assets, list) or not assets or len(assets) > 2
            or len(set(assets)) != len(assets) or any(a not in ("BTC", "ETH") for a in assets)):
        raise ValueError("assets requires unique BTC/ETH selectors.")
    if not isinstance(quotes, tuple) or not quotes or len(set(quotes)) != len(quotes):
        raise ValueError("quotes requires explicit unique native quote selectors.")
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 32
            or any(not isinstance(source, str) or not 1 <= len(source) <= 256
                   or not source.isprintable() for source in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("sources requires 1 to 32 unique source identifiers.")
    if any(quote not in QUOTES for quote in quotes):
        raise ValueError("quotes requires explicit native EUR/USD/USDT/USDC selectors.")
    if now is not None and (type(now) is not int or not 0 <= now <= 253402300799):
        raise ValueError("now requires nonnegative integer UTC epoch seconds.")
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
            saved = store.save_result(result, "book_capture", receipt, data_mode="live")
            observations += saved["inserted_observations"]
            reports.append({"source": source, "captured_books": len(result.books), "inserted_books": written,
                            "inserted_observations": saved["inserted_observations"],
                            "warnings": result.warnings, "errors": result.errors})
        except Exception as exc:
            reports.append({"source": source, "captured_books": 0, "inserted_books": 0, "inserted_observations": 0,
                            "warnings": [], "errors": [public_error(exc)]})
    return {"inserted_books": inserted, "inserted_observations": observations, "reports": reports, "data_mode": "live",
            "whole_book_completeness": "unknown", "source_access_qualified": False}
