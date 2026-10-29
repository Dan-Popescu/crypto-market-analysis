"""One bounded, native-market dossier from a single physical SQLite snapshot.

This read boundary composes the existing pure context and stored calculators.
It never calls MCP, collects data, changes a shared Store, or guesses missing fees.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict

from .context import market_context
from .evidence import canonical_json, content_hash
from .operational_reads import upcoming_calendar_snapshot
from .reporting import bounded_as_of, bounded_integers, bounded_object
from .storage import Store
from .stored_analysis import analyze_stored_derivatives, analyze_stored_quotes, analyze_stored_venues

MAX_CANDLES = 100_000
MAX_RESULT_BYTES = 32 * 1024 * 1024


def _fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"Unsupported {name} request fields.")


def _source(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not value.isprintable():
        raise ValueError("Dossier sources require printable identifiers of 1–100 characters.")


def _sources(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("Dossier source selections require 1–16 unique sources.")
    for item in value:
        _source(item)
    if len(set(value)) != len(value):
        raise ValueError("Dossier sources must be unique.")


def _validate(request):
    bounded_object(request, "analysis dossier request")
    if len(canonical_json(request).encode()) > 65536:
        raise ValueError("Analysis dossier request exceeds the 64 KiB limit.")
    request = deepcopy(request)
    _fields(request, {"data_mode", "markets", "calendar"}, "dossier")
    if request.get("data_mode") not in ("live", "import", "demo"):
        raise ValueError("Analysis dossier requires explicit data_mode live, import or demo.")
    markets = request.get("markets")
    if not isinstance(markets, list) or not 1 <= len(markets) <= 4:
        raise ValueError("Analysis dossier requires 1–4 explicit native markets.")
    identities = set()
    for market in markets:
        _fields(market, {"asset", "source", "quote", "lookbacks", "venues", "derivatives", "quotes"}, "market")
        if market.get("asset") not in ("BTC", "ETH") or market.get("quote") not in ("EUR", "USD"):
            raise ValueError("Dossier markets require BTC or ETH and separate native EUR or USD quotes.")
        _source(market.get("source"))
        identity = (market["asset"], market["source"], market["quote"])
        if identity in identities:
            raise ValueError("Dossier native market selections must be unique.")
        identities.add(identity)
        if "lookbacks" in market:
            bounded_integers(market["lookbacks"], "lookbacks", 3650, 16)
        if "venues" in market:
            venues = market["venues"]
            _fields(venues, {"sources", "max_age_seconds", "max_skew_seconds", "price_basis",
                             "outlier_threshold_bps", "purchase_costs"}, "venues")
            _sources(venues.get("sources"))
            if "purchase_costs" in venues:
                costs = venues["purchase_costs"]
                _fields(costs, {"budget", "assumptions_by_source", "product_by_source",
                                "purchase_eligibility_by_source"}, "purchase costs")
                if "budget" not in costs:
                    raise ValueError("Dossier purchase costs require an explicit native-quote budget.")
                for key in ("assumptions_by_source", "product_by_source", "purchase_eligibility_by_source"):
                    if key in costs and (not isinstance(costs[key], dict) or set(costs[key]) - set(venues["sources"])):
                        raise ValueError("Dossier cost assumptions must name only explicitly selected venue sources.")
        if "derivatives" in market:
            derivatives = market["derivatives"]
            _fields(derivatives, {"source", "quote", "policy"}, "derivatives")
            _source(derivatives.get("source"))
            quote = derivatives.get("quote")
            if not isinstance(quote, str) or not 1 <= len(quote) <= 120 or not quote.isprintable():
                raise ValueError("Dossier derivatives require their own explicit native quote.")
        if "quotes" in market:
            quotes = market["quotes"]
            _fields(quotes, {"sources", "max_age_seconds", "max_endpoint_skew_seconds"}, "quotes")
            _sources(quotes.get("sources"))
    calendar = request.get("calendar", {})
    _fields(calendar, {"source", "start", "end"}, "calendar")
    if "source" in calendar:
        _source(calendar["source"])
    return request


def _evidence(records, selection, cutoff, watermark):
    return {"as_of": cutoff, "watermark": deepcopy(watermark), "selection": selection,
            "input_hash": content_hash({"selection": selection, "as_of": cutoff, "records": records}),
            "hash_scope": "canonical selection, cutoff and exact consumed records",
            "input_references": ["sha256:" + content_hash(row) for row in records],
            "consumed_records": records, "unknown_inputs": [], "read_only": True,
            "truncation": {"truncated": False, "captured": len(records)}}


def _pin(result, cutoff, watermark):
    evidence = result["stored_inputs"]
    evidence["section_watermark"] = evidence["watermark"]
    evidence["watermark"] = deepcopy(watermark)
    evidence["as_of"] = cutoff
    return result


def _market(session, market, mode, cutoff, remaining):
    selection = {key: market[key] for key in ("asset", "source", "quote")}
    native = {**selection, "data_mode": mode}
    raw = session.capture(market["asset"], cutoff, mode, source=market["source"], quote=market["quote"],
                          profile="candles", candle_limit=max(1, remaining))
    candles = raw["candles"]
    if len(candles) > remaining:
        raise ValueError("Analysis dossier exceeds its shared 100000-candle budget.")
    records = [asdict(row) for row in candles]
    context = market_context(candles, cutoff, market.get("lookbacks"))
    context["stored_inputs"] = _evidence(records, {**native, "lookbacks": market.get("lookbacks")},
                                        cutoff, session.watermark_at_start)
    context["input_truncation"] = raw["truncation"]
    missing = {"status": "not_requested"}
    result = {"selection": native, "market_context": context,
              "venue_disagreement": dict(missing), "venue_costs": dict(missing),
              "derivatives": dict(missing), "quotes": dict(missing)}
    if "venues" in market:
        venues = market["venues"]
        venue_native = {key: native[key] for key in ("asset", "quote", "data_mode")}
        common = {key: venues[key] for key in ("sources", "max_age_seconds", "max_skew_seconds") if key in venues}
        request = {**venue_native, **common, **{key: venues[key] for key in
                   ("price_basis", "outlier_threshold_bps") if key in venues}, "analysis": "disagreement"}
        result["venue_disagreement"] = _pin(analyze_stored_venues(session, request, cutoff),
                                            cutoff, session.watermark_at_start)
        if "purchase_costs" in venues:
            request = {**venue_native, **common, **venues["purchase_costs"], "analysis": "purchase_costs"}
            result["venue_costs"] = _pin(analyze_stored_venues(session, request, cutoff),
                                        cutoff, session.watermark_at_start)
    if "derivatives" in market:
        request = {"asset": market["asset"], "data_mode": mode, **market["derivatives"]}
        result["derivatives"] = _pin(analyze_stored_derivatives(session, request, cutoff),
                                    cutoff, session.watermark_at_start)
    if "quotes" in market:
        request = {"asset": market["asset"], "quote": market["quote"], "data_mode": mode, **market["quotes"]}
        result["quotes"] = _pin(analyze_stored_quotes(session, request, cutoff), cutoff, session.watermark_at_start)
    return result, len(candles)


def build_analysis_dossier(store: Store, request: dict, as_of: int | None = None) -> dict:
    """Freeze 1–4 explicit BTC/ETH EUR/USD contexts plus requested stored sections.

    Derivatives declare an independent native source/quote. Venue costs require
    an explicit budget; absent fee/product/eligibility assumptions remain unknown.
    Calendar uses the independent reader's known-at revision/cancellation rules.
    At most 100 calendar rows, 100000 total candles, 5000 derivative vintages per
    market and 16 sources per selector are accepted; excess fails without a cap.
    The complete returned evidence is bounded to 32 MiB and suitable for frozen
    presentation paging. No connection or live cursor escapes this operation.
    """
    request = _validate(request)
    cutoff = bounded_as_of(as_of)
    if cutoff > 253402300799:
        raise ValueError("Dossier as_of is outside the supported UTC range.")
    for key in ("start", "end"):
        if key in request.get("calendar", {}):
            value = bounded_as_of(request["calendar"][key])
            if value > 253402300799:
                raise ValueError("Dossier calendar bounds are outside the supported UTC range.")
    markets = []
    candle_count = 0
    with store.read_session() as session:
        for market in request["markets"]:
            result, count = _market(session, market, request["data_mode"], cutoff, MAX_CANDLES - candle_count)
            markets.append(result)
            candle_count += count
        with session.connection() as con:
            calendar = upcoming_calendar_snapshot(con, request["data_mode"], as_of=cutoff,
                                                  limit=100, **request.get("calendar", {}))
        if calendar["pagination"]["next_cursor"] is not None:
            raise ValueError("Analysis dossier calendar exceeds 100 rows; narrow its source or date range.")
        calendar["status"] = "available" if calendar["rows"] else "missing"
        calendar["stored_inputs"] = _evidence(calendar["rows"], calendar["selection"], cutoff,
                                               session.watermark_at_start)
        frozen = {"schema_version": 1, "as_of": cutoff, "data_mode": request["data_mode"],
                  "request": request, "watermark": deepcopy(session.watermark_at_start),
                  "markets": markets, "calendar": calendar, "read_only": True,
                  "snapshot_basis": "one mode=ro query_only SQLite read transaction; WAL commits visible at start",
                  "resource_limits": {"markets": 4, "total_candles": MAX_CANDLES, "calendar_rows": 100,
                                      "derivative_rows_per_market": 5000, "sources_per_selector": 16,
                                      "request_bytes": 65536, "result_bytes": MAX_RESULT_BYTES,
                                      "sqlite_returned_bytes": 64 * 1024 * 1024,
                                      "sqlite_vm_steps": 50_000_000, "read_session_seconds": 15},
                  "limitations": ["Native EUR, USD and derivatives quotes remain separate; no conversion or blended prices.",
                                  "Missing data and unspecified fee, product or eligibility assumptions remain unknown.",
                                  "Hot SQLite evidence only; archived history needs an explicit archive workflow.",
                                  "Physical watermarks describe this read snapshot, not durable database row identities."]}
        # Hash the entire frozen request, calculations and exact retained evidence.
        frozen["input_hash"] = content_hash(frozen)
        frozen["snapshot_id"] = frozen["input_hash"][:24]
        encoded = canonical_json(frozen)
        if len(encoded.encode()) > MAX_RESULT_BYTES:
            raise ValueError("Analysis dossier exceeds its 32 MiB complete-result budget; narrow the request.")
    # Return only plain detached JSON; callers can retain every detail for paging.
    return json.loads(encoded)
