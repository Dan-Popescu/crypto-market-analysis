"""Capability-aware collection of public market data; timestamps are epoch seconds."""

from __future__ import annotations

import math
import re
import time
from typing import Any

from .models import Candle, CollectionResult, Observation
from .venues import VENUES

QUOTES = ("USD", "USDT", "USDC", "EUR")


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
    ts = int(book.get("timestamp") or now * 1000) // 1000
    if ts > now:
        return []
    meta = {
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "mid": mid,
        "quote": quote,
        "timestamp_source": "exchange" if book.get("timestamp") else "observation",
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
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hH])\s*", interval)
    if not match:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) and value > 0 else None


def _cap(client: Any, name: str) -> bool:
    return bool((getattr(client, "has", {}) or {}).get(name))


def collect_exchange(
    venue: str,
    assets: list[str],
    since: int | None = None,
    now: int | None = None,
    include: tuple[str, ...] = ("candles", "derivatives", "liquidity"),
    max_pages: int = 20,
    *,
    client: Any = None,
) -> CollectionResult:
    live_clock = now is None
    now = int(time.time()) if live_clock else int(now)
    result = CollectionResult(venue)
    if venue not in VENUES:
        result.errors.append(f"Unknown venue: {venue}")
        return result
    if client is None:
        try:
            import ccxt

            client = getattr(ccxt, venue)(
                {"enableRateLimit": True, "timeout": 10000, "requests_trust_env": True}
            )
        except Exception as exc:
            result.errors.append(f"{venue} client: {type(exc).__name__}: {exc}")
            return result
    try:
        markets = client.load_markets()
    except Exception as exc:
        result.errors.append(f"{venue} load_markets: {type(exc).__name__}: {exc}")
        return result
    if not markets:
        result.errors.append(f"{venue} load_markets: empty market catalogue; source may be unavailable")
        return result
    for asset in dict.fromkeys(a.upper() for a in assets):
        spot = select_market(markets, asset, quotes=VENUES[venue].preferred_quotes)
        swap = select_market(markets, asset, "swap", quotes=VENUES[venue].preferred_quotes)
        if not spot:
            result.warnings.append(f"{asset}: no active supported spot market")
        if spot and "candles" in include:
            if not _cap(client, "fetchOHLCV"):
                result.warnings.append(f"{asset}: fetchOHLCV unsupported or requires credentials")
            else:
                cursor = since * 1000 if since is not None else None
                pages = 0
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
                        result.candles.extend(
                            parse_candles(
                                rows, venue, asset, spot["quote"], now, since, binance_raw=raw_binance
                            )
                        )
                        if not rows or since is None:
                            break
                        next_cursor = max(int(r[0]) for r in rows if r and r[0] is not None) + 3600000
                        if (cursor is not None and next_cursor <= cursor) or next_cursor >= now * 1000:
                            break
                        cursor = next_cursor
                    else:
                        result.warnings.append(
                            f"{asset}: candle pagination limit reached; history may be incomplete"
                        )
                except Exception as exc:
                    result.errors.append(f"{asset} fetchOHLCV: {type(exc).__name__}: {exc}")
        if spot and "liquidity" in include:
            if not _cap(client, "fetchOrderBook"):
                result.warnings.append(f"{asset}: fetchOrderBook unsupported or requires credentials")
            else:
                try:
                    book = client.fetch_order_book(spot["symbol"], limit=VENUES[venue].book_limit)
                    if live_clock:
                        now = int(time.time())
                    obs = summarize_book(book, venue, asset, spot["quote"], now)
                    result.observations.extend(obs)
                    if not obs:
                        result.warnings.append(f"{asset}: empty, crossed or future-dated order book")
                except Exception as exc:
                    result.errors.append(f"{asset} fetchOrderBook: {type(exc).__name__}: {exc}")
        if "derivatives" in include:
            if not swap:
                result.warnings.append(f"{asset}: no active supported swap market")
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
                    result.warnings.append(f"{asset}: {capability} unsupported or requires credentials")
                    continue
                try:
                    data = getattr(client, method)(symbol)
                    if live_clock:
                        now = int(time.time())
                    ts = int(data.get("timestamp") or now * 1000) // 1000
                    if ts > now:
                        result.warnings.append(f"{asset} {capability}: future timestamp ignored")
                        continue
                    detail = {
                        **metadata,
                        "timestamp_source": "exchange" if data.get("timestamp") else "observation",
                    }
                    if capability == "fetchFundingRate":
                        detail["funding_interval"] = data.get("interval")
                        detail["funding_interval_hours"] = funding_interval_hours(data)
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
                                None,
                                {**detail, "value_currency": "unknown"},
                            )
                        )
                except Exception as exc:
                    result.errors.append(f"{asset} {capability}: {type(exc).__name__}: {exc}")
            if _cap(client, "fetchFundingRateHistory"):
                try:
                    history = client.fetch_funding_rate_history(
                        symbol, since=since * 1000 if since is not None else None, limit=100
                    )
                    if live_clock:
                        now = int(time.time())
                    if len(history) >= 100:
                        result.warnings.append(f"{asset}: funding history limited to 100 records")
                    for entry in history:
                        ts = int(entry["timestamp"]) // 1000
                        if (
                            ts <= now
                            and (since is None or ts >= since)
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
                                    },
                                )
                            )
                except Exception as exc:
                    result.errors.append(f"{asset} fetchFundingRateHistory: {type(exc).__name__}: {exc}")
            else:
                result.warnings.append(
                    f"{asset}: fetchFundingRateHistory unsupported or requires credentials"
                )
    result.candles = list({(c.asset, c.quote, c.open_time): c for c in result.candles}.values())
    return result
