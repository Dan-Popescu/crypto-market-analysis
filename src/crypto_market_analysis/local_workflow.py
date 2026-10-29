"""Explicit one-shot local collection, qualification and native-market reports."""

from __future__ import annotations

import time
from dataclasses import replace
from hashlib import sha256

from .collectors import ExchangeClients
from .config import Settings
from .engine import Engine
from .pipeline import collect
from .venues import VENUES

NATIVE_MARKETS = ("BTC/EUR", "ETH/EUR", "BTC/USD", "ETH/USD")
_FIELDS = {
    "refresh", "data_mode", "as_of", "sources", "markets", "market_sources",
    "required_history_days", "freshness_seconds", "max_gap_hours", "max_gap_details",
    "max_evidence_rows", "max_query_rows", "max_storage_bytes", "max_pages", "history_days",
    "max_logical_operations", "capture_tasks", "capture_books", "official_context",
    "analyses", "issue_assessments", "idempotency_key",
}


def _integer(request, name, default, low, high):
    value = request.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}.")
    return value


def _selection(request, name, default, allowed):
    values = request.get(name, default)
    if (not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(v, str) or v not in allowed for v in values)
            or len(set(values)) != len(values)):
        raise ValueError(f"{name} must contain distinct supported values.")
    return list(values)


class _OperationBudget:
    """Count adapter calls, including catalogue loads; HTTP fan-out is unknown."""

    def __init__(self, clients, limit):
        self.pool, self.limit = clients, limit
        self.logical_operations = self.catalogue_operations = 0
        self._venue_locks = clients._venue_locks

    def client(self, venue):
        return _BudgetedClient(self.pool.client(venue), self)

    def markets(self, venue, client):
        return self.pool.markets(venue, client)

    def consume(self, method):
        if self.logical_operations >= self.limit:
            raise RuntimeError("The local workflow logical-operation budget is exhausted.")
        self.logical_operations += 1
        self.catalogue_operations += method == "load_markets"

    def report(self):
        return {"max_logical_operations": self.limit,
                "logical_operations": self.logical_operations,
                "catalogue_operations": self.catalogue_operations,
                "http_request_count": None,
                "count_basis": "Public adapter calls; each may fan out into multiple HTTP requests."}


class _BudgetedClient:
    def __init__(self, client, budget):
        self._client, self._budget = client, budget

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if callable(value) and (name == "load_markets" or name.startswith(("fetch_", "public_get_"))):
            def invoke(*args, **kwargs):
                self._budget.consume(name)
                return value(*args, **kwargs)
            return invoke
        return value


def _capture(settings, store, request, clients, http_client):
    budget = _OperationBudget(clients, request["max_logical_operations"])
    result = {"market": None, "books": None, "official_context": None, "errors": []}
    tasks = [task for task in request["capture_tasks"]
             if task != "liquidity" or not request["capture_books"]]
    if tasks:
        try:
            collections = []
            for market in request["markets"]:
                asset, quote = market.split("/")
                scoped = replace(settings, assets=[asset], quote_currencies=[quote],
                                 venues=request.get("market_sources", {}).get(market, settings.venues))
                collected = collect(scoped, store, tasks=tuple(tasks),
                                    days=request["history_days"], clients=budget)
                collections.append({"market": market, **collected})
            result["market"] = {
                "status": "error" if all(row["status"] == "error" for row in collections)
                else "partial" if any(row["status"] in ("error", "partial") for row in collections) else "ok",
                "markets": collections,
                "tasks": tasks,
            }
        except Exception as exc:
            result["errors"].append(f"Market collection: {type(exc).__name__}: {exc}")
    if request["capture_books"]:
        from .collectors import capture_books

        try:
            books = []
            for market in request["markets"]:
                asset, quote = market.split("/")
                books.append({"market": market, **capture_books(
                    store, sources=request.get("market_sources", {}).get(market, settings.venues),
                    assets=[asset], quotes=(quote,), clients=budget)})
            result["books"] = {"markets": books,
                               "reports": [row for value in books for row in value["reports"]]}
        except Exception as exc:
            result["errors"].append(f"Book collection: {type(exc).__name__}: {exc}")
    if request["official_context"] is not None:
        from .official_context import capture_official_context

        try:
            context = capture_official_context(client=http_client, clients=budget,
                                               venues=tuple(settings.venues),
                                               **request["official_context"])
            if context.get("fx_reference") is not None:
                store.save_local_record("fx_reference", context["fx_reference"])
            for row in context.get("stablecoin_prices", []):
                store.save_local_record("stablecoin_price", row)
            result["official_context"] = context
        except Exception as exc:
            result["errors"].append(f"Official context: {type(exc).__name__}: {exc}")
    result["budget"] = budget.report()
    return result


def _normalize(settings, request):
    if not isinstance(request, dict) or set(request) - _FIELDS:
        raise ValueError("Unknown local workflow request fields.")
    normalized = dict(request)
    for field in ("refresh", "capture_books", "issue_assessments"):
        if type(request.get(field, False)) is not bool:
            raise ValueError(f"{field} must be a boolean.")
        normalized[field] = request.get(field, False)
    mode = request.get("data_mode", "live")
    if mode not in ("live", "demo", "import"):
        raise ValueError("data_mode must be live, import or demo.")
    normalized["data_mode"] = mode
    if normalized["refresh"] and (mode != "live" or request.get("as_of") is not None):
        raise ValueError("Refresh requires live mode and the actual current clock, without as_of.")
    if request.get("as_of") is not None and (type(request["as_of"]) is not int
                                           or not 0 <= request["as_of"] <= int(time.time())):
        raise ValueError("as_of must be a nonfuture integer UTC epoch second.")
    sources = request.get("sources", settings.venues)
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 16
            or any(not isinstance(v, str) or not v or len(v) > 64 for v in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("sources must contain 1–16 distinct source names.")
    if normalized["refresh"] and set(sources) - set(VENUES):
        raise ValueError("Refresh sources must be supported public venues.")
    normalized["sources"] = sources
    normalized["markets"] = _selection(request, "markets", NATIVE_MARKETS, NATIVE_MARKETS)
    for name, default, low, high in (
        ("required_history_days", 730, 1, 3650),
        ("freshness_seconds", 10800, 0, 31536000),
        ("max_gap_hours", 0, 0, 87600),
        ("max_gap_details", 100, 0, 1000),
        ("max_evidence_rows", 256, 1, 10000),
        ("max_query_rows", 1_000_000, 1, 2_000_000),
    ):
        normalized[name] = _integer(request, name, default, low, high)
    if "max_storage_bytes" in request:
        normalized["max_storage_bytes"] = _integer(request, "max_storage_bytes", 0, 1, 2**63 - 1)
    market_sources = request.get("market_sources", {})
    if "market_sources" in request:
        if not isinstance(market_sources, dict) or set(market_sources) - set(normalized["markets"]):
            raise ValueError("market_sources must map selected markets to explicit source subsets.")
        for market, subset in market_sources.items():
            if not isinstance(subset, list):
                raise ValueError("market_sources values must be lists of explicit source IDs.")
            _selection({market: subset}, market, [], sources)
    normalized["history_days"] = _integer(request, "history_days", settings.history_days, 1, 3650)
    normalized["max_pages"] = _integer(request, "max_pages", settings.max_pages, 1, 100)
    normalized["max_logical_operations"] = _integer(request, "max_logical_operations", 128, 1, 4096)
    tasks = request.get("capture_tasks", ["market"])
    if (not isinstance(tasks, list) or any(t not in ("market", "liquidity") for t in tasks)
            or len(set(tasks)) != len(tasks)):
        raise ValueError("capture_tasks supports distinct market and liquidity tasks.")
    normalized["capture_tasks"] = tasks
    normalized["analyses"] = _selection(request, "analyses", ["quotes", "derivatives", "venues"],
                                         ("quotes", "derivatives", "venues"))
    official = request.get("official_context")
    if official is not None:
        if (not isinstance(official, dict) or set(official) - {"include", "max_requests"}
                or len(sources) > 4):
            raise ValueError("official_context accepts include and max_requests, with at most four venues.")
        official = {"include": tuple(_selection(official, "include", ["fx", "stablecoins"],
                                               ("fx", "stablecoins"))),
                    "max_requests": _integer(official, "max_requests", 12, 1, 32)}
    normalized["official_context"] = official
    if not normalized["refresh"] and (normalized["capture_books"] or official is not None):
        raise ValueError("Public capture requires explicit refresh=true.")
    if normalized["refresh"] and not tasks and not normalized["capture_books"] and official is None:
        raise ValueError("Refresh requires at least one explicit public capture operation.")
    if normalized["issue_assessments"]:
        key = request.get("idempotency_key")
        if not isinstance(key, str) or not key.strip() or len(key.encode()) > 160:
            raise ValueError("Issuance requires a nonempty idempotency_key of at most 160 UTF-8 bytes.")
    elif request.get("idempotency_key") is not None:
        raise ValueError("An idempotency_key requires explicit issue_assessments=true.")
    return normalized


def _stored_analyses(store, request, asset, quote, source, as_of):
    from .stored_analysis import (
        analyze_stored_derivatives,
        analyze_stored_quotes,
        analyze_stored_venues,
    )

    base = {"asset": asset, "quote": quote, "data_mode": request["data_mode"]}
    calls = {
        "derivatives": (analyze_stored_derivatives, {**base, "source": source}),
        "quotes": (analyze_stored_quotes, {**base, "sources": list(dict.fromkeys([source, "ecb"]))}),
        "venues": (analyze_stored_venues, {**base, "sources": request["sources"],
                                             "analysis": "disagreement"}),
    }
    results = {}
    for name in request["analyses"]:
        operation, payload = calls[name]
        try:
            results[name] = operation(store, payload, as_of)
        except Exception as exc:
            results[name] = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    return results


def _issuance(engine, request, asset, quote, source, *, create=False):
    from .evidence import content_hash
    from .journal import AssessmentJournal

    if not request["issue_assessments"]:
        return None
    identity = sha256(f"{source}:{asset}/{quote}".encode()).hexdigest()[:20]
    key = f"{request['idempotency_key']}:{identity}"
    payload = {"asset": asset, "source": source, "quote": quote,
               "as_of": request.get("as_of"), "plan_config": None, "data_mode": engine.mode}
    original = AssessmentJournal.open_existing(engine.store).original_for_request(key, content_hash(payload))
    if original is not None:
        return {"status": "original", "assessment": original}
    if create:
        return {"status": "issued", "assessment": engine.issue_assessment(
            key, asset, source, quote, request.get("as_of"))}
    return None


def run_local_workflow(settings: Settings, request: dict, *, clients=None,
                       http_client=None, clock=time.time) -> dict:
    """Return a local report; only explicit refresh/issuance write market/journal data.

    ``clock`` controls offline report cutoffs only. Refresh and journal issuance
    retain their actual wall clock. The caller owns injected public clients and
    report-file output. No scheduler, listener, notification or order is started.
    """
    from .accumulation_brief import build_accumulation_brief
    from .qualification import qualify_sources

    request = _normalize(settings, request)
    engine = Engine(settings, request["data_mode"])
    capture = {"status": "not_requested", "network_requested": False}
    if request["refresh"]:
        selected = [market.split("/") for market in request["markets"]]
        scoped = replace(settings, venues=request["sources"],
                         assets=list(dict.fromkeys(asset for asset, _ in selected)),
                         quote_currencies=list(dict.fromkeys(quote for _, quote in selected)),
                         max_pages=request["max_pages"], workers=1, options=False, context=False,
                         liquidity_venues=None)
        if clients is None:
            with ExchangeClients() as owned:
                capture = _capture(scoped, engine.store, request, owned, http_client)
        else:
            capture = _capture(scoped, engine.store, request, clients, http_client)
        capture["network_requested"] = True
    as_of = int(time.time()) if request["refresh"] else request.get("as_of", int(clock()))
    if type(as_of) is not int or not 0 <= as_of <= int(time.time()):
        raise ValueError("The report clock must be a nonfuture integer UTC epoch second.")
    qualification_request = {name: request[name] for name in (
        "sources", "markets", "market_sources", "data_mode", "required_history_days",
        "freshness_seconds", "max_gap_hours", "max_gap_details", "max_evidence_rows", "max_query_rows",
        "max_storage_bytes"
    ) if name in request}
    qualification = qualify_sources(engine.store, qualification_request, as_of)
    reports = []
    for row in qualification["markets"]:
        asset, quote = row["market"].split("/")
        source = row["source"]
        report = {"market": row["market"], "source": source, "data_mode": engine.mode,
                  "status": row["status"], "qualification": row, "analyses": {},
                  "accumulation_brief": None, "issuance": {"status": "not_requested"}}
        demo_only = (engine.mode == "demo" and not request["refresh"]
                     and row["reasons"] == ["synthetic_data_cannot_qualify_a_real_source"]
                     and row["coverage"]["missing_hours"] == 0
                     and row["freshness"]["status"] == "fresh")
        allowed = row["status"] == "pass" or demo_only
        report["analytics_gate"] = {"allowed": allowed,
                                    "basis": "synthetic_demonstration_only" if demo_only else "stored_data_qualification",
                                    "real_source_qualified": row["status"] == "pass" and engine.mode != "demo"}
        original = _issuance(engine, request, asset, quote, source)
        if original is not None:
            report["issuance"] = original
        if allowed:
            try:
                captured = engine.capture_inputs(asset, source, quote, as_of, profile="candles")
                if captured.watermark["candles"] != qualification["watermark"]["candles"]:
                    raise ValueError("Stored candle versions changed after qualification; retry the workflow.")
                report["accumulation_brief"] = build_accumulation_brief(captured)
                report["analyses"] = _stored_analyses(engine.store, request, asset, quote, source, as_of)
                if request["issue_assessments"] and original is None:
                    report["issuance"] = _issuance(engine, request, asset, quote, source, create=True)
            except Exception as exc:
                report.update(status="unavailable", error=f"{type(exc).__name__}: {exc}",
                              accumulation_brief=None, analyses={})
                report["analytics_gate"].update(allowed=False, failure="analysis_failed")
                if request["issue_assessments"] and original is None:
                    report["issuance"] = {"status": "suppressed", "reason": "analysis_failed"}
        elif request["issue_assessments"] and original is None:
            report["issuance"] = {"status": "suppressed", "reason": "qualification_failed"}
        reports.append(report)
    passed = sum(row["status"] == "pass" for row in reports)
    available = sum(row["accumulation_brief"] is not None for row in reports)
    status = "pass" if passed == len(reports) and reports else "partial" if available else "unavailable"
    if request["refresh"] and status == "pass":
        failed = bool(capture["errors"])
        failed |= (capture.get("market") or {}).get("status") in ("error", "partial")
        failed |= any(r.get("errors") for r in (capture.get("books") or {}).get("reports", []))
        official = capture.get("official_context") or {}
        failed |= bool(official.get("errors")) or official.get("status") in ("partial", "unavailable", "error")
        if failed:
            status = "partial"
    return {"schema_version": 1, "kind": "local_workflow", "status": status,
            "as_of": as_of, "data_mode": engine.mode, "refresh": request["refresh"],
            "collection": capture, "qualification": qualification, "markets": reports,
            "issuance_requested": request["issue_assessments"],
            "limitations": ["Native markets and data modes remain independent; missing inputs stay unknown.",
                            "Qualification describes stored data; it does not establish reuse rights or purchase eligibility.",
                            "Adapter operation limits do not establish a literal HTTP-request limit."]}
