"""Bounded cross-asset application operations shared by CLI and read-only MCP.

Capabilities describe implemented research semantics, never provider acceptance.
Every series selection pins the native currency and data mode. Legacy crypto
records remain under their original contracts and are not reinterpreted here.
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
from pathlib import Path

from . import identity, prices, research, sessions

MAX_SERIES = 1_000
MAX_ROWS = 10_000
MAX_SPAN = 3660 * 86400


def market_capabilities() -> dict:
    """Publish tested semantics separately from source and operational acceptance."""
    return {
        "schema": "market-capabilities.v1",
        "status": "experimental",
        "qualification": {"synthetic_acceptance": True, "qualified_live_sources": [],
                          "real_market_end_to_end": False,
                          "legacy_crypto": "Existing cryptocurrency operations retain their own qualification."},
        "features": {
            "identity": {"supported": True, "semantics": ["stable instrument and listing IDs", "bitemporal aliases", "delisting and membership history"]},
            "calendars": {"supported": True, "semantics": ["versioned sourced sessions", "native intervals", "closed, missing and not-yet-due observations", "reference publication schedules"]},
            "price_analysis": {"supported": True, "bases": ["raw", "split_adjusted", "total_return", "continuous"],
                               "conditions": ["explicit annualization", "method-specific positive-price gates", "native currency and unit"]},
            "cash_plan": {"supported": True, "forms": ["share", "fund", "spot"],
                          "conditions": ["raw prices", "supported instrument asset-class/form pairing", "complete declared actions", "cash distributions", "explicit fees", "point-in-time fills"]},
            "reference_cash_fills": {"supported": False, "reason": "Publication references are not executable prices."},
            "futures_cash_plan": {"supported": False, "reason": "The cash-funded plan does not model futures margin, collateral or settlement."},
            "futures_valuation": {"supported": True, "conditions": ["explicit contract unit, multiplier and settlement", "separate futures valuation model"]},
            "continuous_builder": {"supported": True, "conditions": ["explicit versioned chain and roll method", "point-in-time chain selection", "synthetic research prices"]},
            "prospective": {"supported": True, "conditions": ["frozen series and calendar identity", "explicit horizon semantics", "receipt cutoff and observed outcomes"]},
            "continuous_cash_fills": {"supported": False, "reason": "Synthetic continuous contracts are not executable fills."},
            "currency_conversion": {"supported": False, "reason": "Research preserves native currencies; no FX conversion is implied."},
            "live_collection": {"supported": False, "reason": "Provider acceptance and usage rights require source-specific evidence."},
            "order_execution": {"supported": False, "reason": "These operations are hypothetical research."},
            "storage": {"supported": True, "semantics": ["receipt cutoffs", "separate live/import/demo modes", "immutable native series"]},
        },
        "limits": {"instruments": 1_000, "series_per_bundle": MAX_SERIES,
                   "rows_per_request": MAX_ROWS, "plan_events_per_kind": 1000, "request_span_seconds": MAX_SPAN},
    }


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - set(optional):
        raise ValueError("Request has missing or unknown fields.")


def _clock(value, label):
    if type(value) is not int or not -62135596800 <= value <= 253402300799:
        raise ValueError(f"{label} must be integer UTC epoch seconds.")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or value != value.strip():
        raise ValueError(f"{label} must be a trimmed string of 1–256 characters.")
    return value


def _window(request):
    start, end = _clock(request["start"], "start"), _clock(request["end"], "end")
    if not start < end <= start + MAX_SPAN:
        raise ValueError("Request requires start < end and a span of at most 3660 days.")
    return start, end


def _store(database):
    if isinstance(database, (str, Path)):
        from .storage import CrossAssetStore

        return CrossAssetStore(database)
    return database


def _failure(status, exc, **metadata):
    return {"schema": "market-result.v1", "status": status, "reason": str(exc), **metadata}


def _effective(row, start, end, at, cutoff):
    return row["known_at"] <= cutoff and row[start] <= at and (row[end] is None or at < row[end])


class _SeriesIdentity:
    """Apply native series constraints to identity-owned indexed resolution."""

    def __init__(self, registry):
        self.resolver = identity.RegistryResolver(registry)
        self.registry = self.resolver.registry
        self.instruments = {row["id"]: row for row in self.registry["instruments"]}

    def resolve(self, descriptor, at, cutoff):
        resolved = self.resolver.resolve_alias(
            provider=descriptor["provider"], symbol=descriptor["provider_symbol"],
            effective_at=at, as_of=cutoff, listing_id=descriptor["listing_id"],
        )
        alias, listing = resolved["alias"], resolved["listing"]
        if (alias["instrument_id"], alias["listing_id"]) != (descriptor["instrument_id"], descriptor["listing_id"]):
            raise ValueError("Series descriptor does not match its resolved provider alias.")
        instrument = self.instruments[alias["instrument_id"]]
        if listing is not None and (listing["native_currency"] != descriptor["native_currency"]
                                    or listing["calendar_id"] != descriptor["calendar_id"]):
            raise ValueError("Series native currency or calendar differs from its listing.")
        if instrument["form"] == "future" and instrument["contract"]["unit"] != descriptor["unit"]:
            raise ValueError("Series unit differs from its futures contract.")
        if instrument["form"] == "reference" and descriptor["interval"] != "reference":
            raise ValueError("Reference instruments require a reference publication interval.")
        if descriptor["price_basis"] in ("split_adjusted", "total_return") and instrument["form"] not in ("share", "fund"):
            raise ValueError("Corporate-action adjusted bases require a share or fund instrument.")
        if descriptor["price_basis"] == "continuous" and instrument["form"] != "future":
            raise ValueError("Continuous price basis requires a futures instrument.")
        return {"instrument": instrument, "listing": listing, "alias": alias}


def validate_capture_metadata(registry, calendars, descriptor, *, start, end, as_of):
    """Resolve a capture's declared metadata before any provider request.

    No observation is invented for preflight. Complete returned observations
    subsequently pass the ordinary bundle validator at their own receipt clocks.
    """
    index = _SeriesIdentity(registry)
    registry = index.registry
    _clock(as_of, "as_of")
    _window({"start": start, "end": end})
    if not isinstance(calendars, list) or not 1 <= len(calendars) <= MAX_SERIES:
        raise ValueError("Capture calendars must be a bounded nonempty list.")
    validated = [sessions.validate_calendar(row) for row in calendars]
    calendar_index = {(row["id"], row["version"]): row for row in validated}
    if len(calendar_index) != len(validated):
        raise ValueError("Capture contains duplicate calendar versions.")
    descriptor = prices.validate_series({"schema": "market-series.v1", "descriptor": descriptor,
                                          "bars": [], "actions": []})["descriptor"]
    calendar = calendar_index.get((descriptor["calendar_id"], descriptor["calendar_version"]))
    if calendar is None or calendar["known_at"] > as_of:
        raise ValueError("Capture requires a known matching calendar version.")
    resolved = index.resolve(descriptor, start, as_of)
    return {"descriptor": descriptor, "identity": resolved, "calendar": calendar}


def validate_bundle(bundle: dict) -> dict:
    """Validate native contracts and their cross-references in a defensive copy.

    Registry and calendar validation runs once per bundle. Alias resolution uses
    indexed candidate groups and every observation's own effective/receipt clocks.
    """
    _fields(bundle, {"schema", "registry", "calendars", "series"})
    if bundle["schema"] != "market-bundle.v1":
        raise ValueError("Unsupported bundle schema; expected market-bundle.v1.")
    if not isinstance(bundle["calendars"], list) or len(bundle["calendars"]) > MAX_SERIES:
        raise ValueError("Bundle calendars must be a bounded list.")
    if not isinstance(bundle["series"], list) or len(bundle["series"]) > MAX_SERIES:
        raise ValueError("Bundle series must contain at most 1000 series.")
    index = _SeriesIdentity(bundle["registry"])
    registry = index.registry
    calendars = [sessions.validate_calendar(row) for row in bundle["calendars"]]
    calendar_index = {(row["id"], row["version"]): row for row in calendars}
    if len(calendar_index) != len(calendars):
        raise ValueError("Bundle contains duplicate calendar versions.")
    normalized, identifiers, grids = [], set(), {}
    for document in bundle["series"]:
        series = prices.validate_series(document)
        descriptor = series["descriptor"]
        key = descriptor["id"]
        if key in identifiers:
            raise ValueError("Bundle contains duplicate series IDs; each mode requires its own series identity.")
        identifiers.add(key)
        calendar = calendar_index.get((descriptor["calendar_id"], descriptor["calendar_version"]))
        if calendar is None:
            raise ValueError("Series references a calendar version absent from the bundle.")
        if descriptor["instrument_id"] not in index.instruments:
            raise ValueError("Series instrument is absent from the registry.")
        if not series["bars"]:
            raise ValueError("Imported series requires at least one observation to resolve its identity.")
        grid_end = max(bar["end"] for bar in series["bars"])
        if descriptor["interval"] == "reference" and any(bar["start"] == bar["end"] == grid_end for bar in series["bars"]):
            # Publication events are points on a half-open calendar query grid.
            grid_end += 1
        bounds = (min(bar["start"] for bar in series["bars"]), grid_end)
        grid_key = (descriptor["calendar_id"], descriptor["calendar_version"], descriptor["interval"], *bounds)
        if grid_key not in grids:
            grid = sessions.expected_observations(calendar, start=bounds[0], end=bounds[1],
                                                   as_of=max(bar["observed_at"] for bar in series["bars"]),
                                                   interval=descriptor["interval"])
            grids[grid_key] = {(row["start"], row["end"]) for row in grid if row["status"] != "unknown"}
        for bar in series["bars"]:
            if (bar["start"], bar["end"]) not in grids[grid_key]:
                raise ValueError("Observation bounds do not match the declared native calendar grid.")
            index.resolve(descriptor, bar["start"], bar["observed_at"])
            if calendar["known_at"] > bar["observed_at"]:
                raise ValueError("Observation predates knowledge of its declared calendar version.")
        normalized.append(series)
    return {"schema": bundle["schema"], "registry": registry, "calendars": calendars, "series": normalized}


def market_registry(database, request: dict, *, as_of: int) -> dict:
    try:
        _fields(request, set(), {"effective_at", "universe", "provider", "symbol", "listing_id"})
        _clock(as_of, "as_of")
        effective_at = _clock(request.get("effective_at", as_of), "effective_at")
        registry = _store(database).registry(as_of=as_of)
        if registry is None:
            return _failure("unavailable", "No registry is available at this cutoff.", as_of=as_of)
        if "provider" in request or "symbol" in request:
            if "provider" not in request or "symbol" not in request or "universe" in request:
                raise ValueError("Alias resolution requires provider and symbol, without universe.")
            result = identity.resolve_alias(registry, provider=request["provider"], symbol=request["symbol"],
                                            effective_at=effective_at, as_of=as_of,
                                            listing_id=request.get("listing_id"))
        else:
            if "listing_id" in request:
                raise ValueError("listing_id requires provider and symbol.")
            result = identity.registry_view(registry, as_of=as_of, effective_at=effective_at,
                                            universe=request.get("universe"))
        return {"schema": "market-result.v1", "status": "ok", "as_of": as_of, "registry": result}
    except ValueError as exc:
        return _failure("invalid_request", exc)


def market_calendar(database, request: dict, *, as_of: int) -> dict:
    try:
        _fields(request, {"calendar_id", "calendar_version", "start", "end"}, {"interval"})
        _clock(as_of, "as_of")
        start, end = _window(request)
        _text(request["calendar_id"], "calendar_id")
        _text(request["calendar_version"], "calendar_version")
        calendar = _store(database).calendar(request["calendar_id"], request["calendar_version"], as_of=as_of)
        if calendar is None:
            return _failure("unavailable", "Calendar version is unavailable at this cutoff.", as_of=as_of)
        coverage = sessions.coverage_report(calendar, [], start=start, end=end, as_of=as_of,
                                             interval=request.get("interval", "session"))
        return {"schema": "market-result.v1", "status": "ok", "as_of": as_of,
                "calendar": calendar, "coverage": coverage}
    except ValueError as exc:
        return _failure("invalid_request", exc)


def _series_inputs(database, request, as_of, extra):
    store = _store(database)
    with store.read_session() if hasattr(store, "read_session") else nullcontext(store) as reader:
        return _series_inputs_from_store(reader, request, as_of, extra)


def _series_inputs_from_store(store, request, as_of, extra):
    _fields(request, {"series_id", "data_mode", "currency", "start", "end"} | extra)
    _clock(as_of, "as_of")
    for key in ("series_id", "currency"):
        _text(request[key], key)
    if request["data_mode"] not in ("live", "import", "demo"):
        raise ValueError("data_mode must be live, import or demo.")
    start, end = _window(request)
    document = store.read_series(request["series_id"], as_of=as_of, data_mode=request["data_mode"],
                                 start=start, end=end, max_rows=MAX_ROWS,
                                 all_vintages=extra == {"plan"})
    if document is None:
        return None
    document = copy.deepcopy(document)
    provenance = document.get("storage_provenance")
    series = prices.validate_series(document)
    descriptor = series["descriptor"]
    if descriptor["id"] != request["series_id"] or descriptor["data_mode"] != request["data_mode"]:
        raise ValueError("Stored series does not match the requested identity and data mode.")
    if descriptor["native_currency"] != request["currency"]:
        raise ValueError("Requested currency differs from the series native currency; conversion is unsupported.")
    registry = store.registry(as_of=as_of)
    calendar = store.calendar(descriptor["calendar_id"], descriptor["calendar_version"], as_of=as_of)
    if registry is None or calendar is None:
        raise ValueError("Series registry or declared calendar version is unavailable at this cutoff.")
    index = _SeriesIdentity(registry)
    resolutions = [index.resolve(descriptor, bar["start"], as_of) for bar in series["bars"]]
    resolved = resolutions[0] if resolutions else index.resolve(descriptor, start, as_of)
    selected = prices.bars_as_of(series, as_of, start=start, end=end)
    coverage = sessions.coverage_report(calendar, selected, start=start, end=end, as_of=as_of,
                                         interval=descriptor["interval"])
    metadata = {"as_of": as_of, "selection": {key: request[key] for key in
                ("series_id", "data_mode", "currency", "start", "end")},
                "descriptor": descriptor, "identity": resolved, "registry": registry,
                "calendar": calendar, "coverage": coverage, "storage_provenance": provenance,
                "capabilities": market_capabilities(), "series": series}
    return series, resolved["instrument"], metadata


def market_analysis(database, request: dict, *, as_of: int) -> dict:
    try:
        inputs = _series_inputs(database, request, as_of, {"annualization"})
        if inputs is None:
            return _failure("unavailable", "Series is unavailable in this data mode and cutoff.", as_of=as_of)
        series, _, metadata = inputs
        result = research.analyze_series(series, as_of=as_of, start=request["start"], end=request["end"],
                                          annualization=request["annualization"])
        return {"schema": "market-result.v1", "status": "ok", **metadata, "analysis": result}
    except ValueError as exc:
        return _failure("invalid_request", exc)


def market_plan(database, request: dict, *, as_of: int) -> dict:
    try:
        inputs = _series_inputs(database, request, as_of, {"plan"})
        if inputs is None:
            return _failure("unavailable", "Series is unavailable in this data mode and cutoff.", as_of=as_of)
        series, instrument, metadata = inputs
        descriptor = series["descriptor"]
        if instrument["form"] in ("future", "reference") or descriptor["price_basis"] != "raw":
            return _failure("unsupported", "Cash plans require supported cash instruments and raw traded prices.", **metadata)
        if not isinstance(request["plan"], dict) or request["plan"].get("currency") != request["currency"]:
            raise ValueError("Plan currency must equal the explicitly selected native currency.")
        for key in ("deposits", "purchases"):
            events = request["plan"].get(key)
            if not isinstance(events, list) or len(events) > 1000:
                raise ValueError(f"Plan {key} must be an array of at most 1000 events.")
            for event in events:
                if not isinstance(event, dict) or "at" not in event:
                    raise ValueError(f"Plan {key} require explicit event clocks.")
                at = _clock(event["at"], f"{key}.at")
                if not request["start"] <= at <= min(request["end"], as_of):
                    raise ValueError("Plan event clocks must be within the selected historical window.")
        result = research.simulate_cash_plan(series, request["plan"], as_of=as_of, instrument=instrument)
        return {"schema": "market-result.v1", "status": "ok", **metadata, "plan": result}
    except ValueError as exc:
        return _failure("invalid_request", exc)
