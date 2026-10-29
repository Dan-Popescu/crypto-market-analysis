"""Bounded public acquisition; a source date never implies a publication instant."""

from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .collectors import ExchangeClients
from .http_accounting import (
    _provider_source,
    http_source_scope,
    instrument_exchange,
    instrument_httpx,
    mark_uninstrumented,
)
from .http_limits import HTTPBudgetExceeded, bounded_public_operation, check_http_budget
from .venues import VENUES

ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_FX_NS = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"
_GESMES_NS = "http://www.gesmes.org/xml/2002-08-01"
_MAX_BYTES = 2 * 1024 * 1024


class _BudgetExhausted(ValueError):
    pass


def _epoch(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 253402300799:
        raise ValueError("Capture time must be a nonnegative supported epoch second.")
    return value


def _decimal(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or len(str(value)) > 100:
        raise ValueError("Price must be a positive bounded decimal.")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Price must be a positive bounded decimal.") from exc
    if not parsed.is_finite() or parsed <= 0 or not -30 <= parsed.adjusted() <= 30:
        raise ValueError("Price must be a positive finite decimal.")
    if len(parsed.as_tuple().digits) > 40:
        raise ValueError("Price exceeds the precision limit.")
    return format(parsed, "f")


def _digest(raw: bytes) -> str:
    if not isinstance(raw, bytes) or len(raw) > _MAX_BYTES:
        raise ValueError("Response must contain at most two megabytes of bytes.")
    return hashlib.sha256(raw).hexdigest()


class OfficialHTTP:
    """One-shot public transport with streamed byte limits and no account credentials."""

    def __init__(self):
        self.client = httpx.Client(
            timeout=10, follow_redirects=False, headers={"User-Agent": "crypto-market-analysis/0.4"}
        )

    def get_bytes(self, url: str) -> bytes:
        source = _provider_source(url) or "ecb"
        instrument_httpx(self.client, source)
        with http_source_scope(source), self.client.stream("GET", url, extensions={"observer_stream": True}) as response:
            response.raise_for_status()
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > _MAX_BYTES:
                    raise ValueError("Response exceeds the two-megabyte limit.")
                chunks.append(chunk)
            return b"".join(chunks)

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8-sig")

    def close(self):
        self.client.close()


def parse_ecb_fx(raw: bytes, observed_at: int, *, data_mode: str = "live") -> dict[str, Any]:
    """Parse the ECB daily Cube XML: USD per EUR, source date only.

    The daily file has no publication timestamp or historical availability proof.
    The publication schedule on the ECB website is not a clock on this observation.
    """
    _epoch(observed_at)
    if data_mode not in {"live", "import", "demo"}:
        raise ValueError("Unsupported data mode.")
    digest = _digest(raw)
    # XML entities and DTDs are outside this tiny public schema, including UTF-16
    # representations which would defeat a plain ASCII substring guard.
    text = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper() or "\x00" in text:
        raise ValueError("DTD/entity declarations and non-UTF8 XML are unsupported.")
    root = ET.fromstring(text)
    if root.tag != f"{{{_GESMES_NS}}}Envelope":
        raise ValueError("Unexpected ECB XML envelope.")
    elements = list(root.iter())
    if len(elements) > 256:
        raise ValueError("ECB XML exceeds the element-count limit.")
    containers = root.findall(f"{{{_FX_NS}}}Cube")
    if len(containers) != 1 or containers[0].attrib:
        raise ValueError("Expected one daily ECB Cube container.")
    days = list(containers[0])
    if len(days) != 1 or days[0].tag != f"{{{_FX_NS}}}Cube" or set(days[0].attrib) != {"time"}:
        raise ValueError("Expected one daily ECB source date.")
    source_date = date.fromisoformat(days[0].attrib["time"])
    if source_date.isoformat() != days[0].attrib["time"]:
        raise ValueError("ECB source date must use YYYY-MM-DD.")
    if source_date > datetime.fromtimestamp(observed_at, ZoneInfo("Europe/Berlin")).date():
        raise ValueError("ECB source date is after capture date.")
    rates = {}
    for row in days[0]:
        if row.tag != f"{{{_FX_NS}}}Cube" or set(row.attrib) != {"currency", "rate"} or len(row):
            raise ValueError("Unexpected daily ECB currency row.")
        currency = row.attrib["currency"]
        if len(currency) != 3 or not currency.isascii() or not currency.isupper() or not currency.isalpha():
            raise ValueError("Invalid ECB currency code.")
        if currency in rates:
            raise ValueError("Duplicate ECB currency row.")
        rates[currency] = _decimal(row.attrib["rate"])
    if "USD" not in rates:
        raise ValueError("Daily ECB XML contains no USD reference.")
    return {
        "record_id": f"ecb:EUR/USD:{source_date}",
        "base": "EUR", "quote": "USD", "value": rates["USD"],
        "event_time": None, "observed_at": observed_at, "materialized_at": observed_at,
        "source": "ecb", "source_url": ECB_FX_URL, "source_date": source_date.isoformat(),
        "source_timezone": "Europe/Berlin", "timestamp_status": "date_only",
        "publication_time": None, "historical_known_at": "unknown",
        "data_version": "sha256:" + digest, "raw_sha256": digest, "hash_scope": "response_bytes",
        "price_type": "indicative_reference", "data_mode": data_mode,
        "executable": False, "source_format": "ecb-eurofxref-daily-xml",
    }


def _receipt(source: str, url: str, raw: bytes, captured: int, scope: str) -> dict:
    return {"source": source, "source_url": url, "raw_sha256": _digest(raw),
            "hash_scope": scope, "captured_at": captured, "observed_at": captured,
            "bytes": len(raw)}


@bounded_public_operation
def capture_official_context(
    *, now: int | None = None, client: Any = None, clients: ExchangeClients | None = None,
    venues: tuple[str, ...] = ("kraken",), data_mode: str = "live",
    include: tuple[str, ...] = ("fx", "stablecoins"), max_requests: int = 12,
) -> dict[str, Any]:
    """Capture latest public evidence only, with per-feed failures and logical-call budgets.

    Injected clients remain caller-owned. FX HTTP injection requires ``get_bytes``;
    re-encoded decoded text is not called a raw response hash. CCXT receipts hash
    the normalized adapter payload and explicitly do not assert wire-byte identity.
    ``now`` is an injected deterministic capture clock, never a historical query.
    The budget counts catalogue and ticker calls; adapter internals may make more
    than one HTTP request when loading a catalogue.
    """
    if data_mode != "live":
        raise ValueError("Automatic official context requires live mode.")
    if (not isinstance(include, (list, tuple)) or not include
            or any(item not in {"fx", "stablecoins"} for item in include)):
        raise ValueError("Include fx and/or stablecoins.")
    if (not isinstance(venues, (list, tuple)) or len(venues) > 4
            or any(venue not in VENUES for venue in venues)):
        raise ValueError("Use at most four supported public venues.")
    if "stablecoins" in include and not venues:
        raise ValueError("Stablecoin capture requires a venue.")
    if type(max_requests) is not int or not 1 <= max_requests <= 32:
        raise ValueError("max_requests must be from 1 to 32.")
    clock = (lambda: _epoch(int(time.time()))) if now is None else (lambda: _epoch(now))
    started = clock()
    result: dict[str, Any] = {
        "status": "unavailable", "observed_at": started, "fx_reference": None,
        "stablecoin_prices": [], "receipts": [], "errors": [], "warnings": [],
        "data_mode": "live", "logical_requests": 0, "max_requests": max_requests,
    }

    def budget():
        check_http_budget()
        if result["logical_requests"] >= max_requests:
            raise _BudgetExhausted("Public logical-request budget exhausted.")
        result["logical_requests"] += 1

    http_stopped = False

    def failed(source: str, exc: Exception):
        nonlocal http_stopped
        http_stopped = http_stopped or isinstance(exc, HTTPBudgetExceeded)
        # Transport exception messages can contain credential-bearing proxy URLs.
        result["errors"].append({"source": source, "status": "unavailable",
                                 "reason": "budget_exhausted" if isinstance(exc, (_BudgetExhausted, HTTPBudgetExceeded)) else "source_failure",
                                 "error": f"{type(exc).__name__}: public source acquisition failed"})

    own_http = client is None and "fx" in include
    own_pool = clients is None and "stablecoins" in include
    try:
        if "fx" in include:
            try:
                if own_http:
                    client = OfficialHTTP()
                budget()
                if not isinstance(client, OfficialHTTP):
                    mark_uninstrumented("ecb")
                raw = client.get_bytes(ECB_FX_URL)
                captured = clock()
                receipt = _receipt("ecb", ECB_FX_URL, raw, captured, "response_bytes")
                result["receipts"].append(receipt)
                result["fx_reference"] = parse_ecb_fx(raw, captured)
            except Exception as exc:
                failed("ecb", exc)
        if "stablecoins" in include:
            if own_pool:
                clients = ExchangeClients()
            for venue in dict.fromkeys(venues):
                if http_stopped:
                    break
                try:
                    budget()
                    exchange = clients.client(venue)
                    instrument_exchange(exchange, venue)
                    with http_source_scope(venue):
                        markets = clients.markets(venue, exchange)
                    if len(markets) > 100000:
                        raise ValueError("Public catalogue exceeds market-count limit.")
                    if not getattr(exchange, "has", {}).get("fetchTicker"):
                        raise ValueError("Public ticker capability unsupported.")
                    candidates = [m for m in markets.values()
                                  if m.get("base") in {"USDT", "USDC"}
                                  and m.get("quote") in {"USD", "EUR"}
                                  and m.get("spot") is True and m.get("active") is not False
                                  and isinstance(m.get("symbol"), str)]
                    by_pair = {}
                    for market in sorted(candidates, key=lambda m: m["symbol"]):
                        by_pair.setdefault((market["base"], market["quote"]), market)
                    if not by_pair:
                        raise ValueError("No active native stablecoin/fiat spot markets.")
                except Exception as exc:
                    failed(venue, exc)
                    continue
                for market in by_pair.values():
                    if http_stopped:
                        break
                    symbol = market["symbol"]
                    try:
                        budget()
                        with http_source_scope(venue):
                            ticker = exchange.fetch_ticker(symbol)
                        captured = clock()
                        raw = json.dumps(ticker, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()
                        # The venue homepage identifies the public adapter source;
                        # method and symbol identify the request without fabricating an endpoint.
                        website = getattr(exchange, "urls", {}).get("www")
                        if not isinstance(website, str):
                            raise ValueError("Public adapter provides no provenance website.")
                        parsed_url = urlsplit(website)
                        if (parsed_url.scheme != "https" or not parsed_url.hostname
                                or parsed_url.username or parsed_url.password):
                            raise ValueError("Public adapter provenance website must use HTTPS without credentials.")
                        receipt = _receipt(venue, website, raw, captured, "normalized_adapter_payload")
                        receipt.update(adapter_method="fetch_ticker", symbol=symbol)
                        result["receipts"].append(receipt)
                        if ticker.get("symbol") not in {None, symbol}:
                            raise ValueError("Ticker symbol differs from requested native market.")
                        value = _decimal(ticker.get("last"))
                        timestamp = ticker.get("timestamp")
                        if timestamp is not None:
                            if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                                    or timestamp < 0 or timestamp > captured * 1000):
                                raise ValueError("Invalid or future exchange timestamp.")
                            event_time = int(timestamp) // 1000
                        else:
                            event_time = None
                        result["stablecoin_prices"].append({
                            "record_id": venue + ":" + symbol,
                            "base": market["base"], "quote": market["quote"], "value": value,
                            "event_time": event_time, "observed_at": captured, "materialized_at": captured,
                            "source": venue, "source_url": website, "symbol": symbol,
                            "market_type": "spot", "adapter_method": "fetch_ticker", "price_field": "last",
                            "timestamp_status": "exchange" if event_time is not None else "unknown",
                            "timestamp_semantics": "adapter_ticker_timestamp_not_proven_last_trade_time"
                            if event_time is not None else "market_timestamp_unknown",
                            "historical_known_at": "unknown", "price_type": "observed_spot",
                            "data_mode": "live", "data_version": "sha256:" + receipt["raw_sha256"],
                            "raw_sha256": receipt["raw_sha256"], "hash_scope": receipt["hash_scope"],
                        })
                    except Exception as exc:
                        failed(venue + ":" + symbol, exc)
    finally:
        if own_http and client is not None:
            client.close()
        if own_pool and clients is not None:
            clients.close()
    result["observed_at"] = clock()
    count = int(result["fx_reference"] is not None) + len(result["stablecoin_prices"])
    result["status"] = "partial" if count and result["errors"] else "available" if count else "unavailable"
    return result
