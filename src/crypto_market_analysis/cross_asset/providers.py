"""Explicit, bounded low-frequency imports and a keyless reference-feed adapter.

Adapter implementation is not provider acceptance. Cataloguing a source makes no
request and does not establish licensing, live access or executable prices.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import time
from datetime import date

import httpx

from ..http_accounting import HTTPAccounting, public_error
from ..http_limits import HTTPBudgetExceeded, HTTPPolicy, http_budget_scope

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
_FIELDS = ("id", "instrument_id", "listing_id", "provider", "provider_symbol", "native_currency",
           "unit", "price_basis", "data_mode", "calendar_id", "calendar_version", "interval",
           "revision_semantics")


def provider_catalog() -> dict:
    """Return detached capabilities, including unresolved acceptance evidence."""
    return {"schema": "cross-asset-providers.v1", "default_provider": None, "providers": [
        {"id": "normalized_import", "implemented": True, "network": False, "credentials": False,
         "intervals": ["session", "reference"], "price_bases": ["raw", "split_adjusted", "total_return"],
         "actions": ["split", "cash_dividend"], "qualification": "conditional_user_contract",
         "historical_universe": "user_supplied_only", "rights": "user_must_supply",
         "production_accepted": False},
        {"id": "fred_graph", "implemented": True, "network": True, "credentials": False,
         "opt_in_required": True, "symbols": ["DCOILWTICO"], "intervals": ["reference"],
         "price_bases": ["raw"], "actions": [], "executable": False,
         "qualification": "unqualified_live_access_and_rights", "production_accepted": False,
         "source_url": "https://fred.stlouisfed.org/series/DCOILWTICO",
         "revision_semantics": "captured_history"},
        {"id": "yahoo_chart", "implemented": False, "qualification": "unknown_adjustment_and_rights",
         "production_accepted": False, "source_url": "https://help.yahoo.com/kb/SLN28256.html"},
        {"id": "stooq_csv", "implemented": False, "qualification": "unknown_adjustment_and_rights",
         "production_accepted": False, "source_url": "https://stooq.com/"}]}


def _epoch(value, name):
    if type(value) is not int or not -62135596800 <= value <= 253402300799:
        raise ValueError(f"{name} requires integer UTC seconds in the supported date range.")
    return value


def _limits(request):
    selected = request.get("limits", {})
    if not isinstance(selected, dict) or set(selected) - {"max_requests", "max_bytes", "max_rows",
                                                         "deadline_seconds"}:
        raise ValueError("Unknown provider limits.")
    limits = {"max_requests": 2, "max_bytes": MAX_BYTES, "max_rows": MAX_ROWS,
              "deadline_seconds": 20.0, **selected}
    for name, maximum in (("max_requests", 3), ("max_bytes", MAX_BYTES), ("max_rows", MAX_ROWS)):
        if type(limits[name]) is not int or not 1 <= limits[name] <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}.")
    deadline = limits["deadline_seconds"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or not 0 < deadline <= 30:
        raise ValueError("deadline_seconds must be finite and between 0 (exclusive) and 30.")
    return limits


def _descriptor(request):
    from .prices import validate_series

    descriptor = request.get("descriptor")
    if not isinstance(descriptor, dict) or any(field not in descriptor for field in _FIELDS):
        raise ValueError("A complete resolved market-series descriptor is required.")
    if descriptor["interval"] not in ("session", "reference"):
        raise ValueError("This adapter only supports session or reference observations.")
    if descriptor["revision_semantics"] != "captured_history":
        raise ValueError("Current imports and graph CSV cannot establish point-in-time history.")
    return validate_series({"schema": "market-series.v1", "descriptor": descriptor,
                            "bars": [], "actions": []})["descriptor"]


def _assessment(descriptor, contract):
    return {"provider": descriptor["provider"], "production_accepted": False,
            "status": "conditional_user_contract" if descriptor["provider"] == "normalized_import"
            else "unqualified_live_access_and_rights",
            "rights": contract.get("rights", "unknown"), "origin": contract.get("origin", "unknown"),
            "rights_verified": False, "historical_universe": "unknown",
            "action_coverage": contract.get("action_coverage", "unknown"),
            "point_in_time_history": False,
            "executable": None if descriptor["interval"] == "session" and descriptor["price_basis"] == "raw"
            else False,
            "execution_basis": "user_asserted_raw_import_only" if descriptor["interval"] == "session"
            and descriptor["price_basis"] == "raw" else "adjusted_or_reference_observations_are_not_traded_fills"}


def _csv_import(text, max_rows):
    if not isinstance(text, str):
        raise ValueError("import_csv must be normalized CSV text.")
    reader = csv.DictReader(io.StringIO(text))
    required = {"start", "end", "open", "high", "low", "close"}
    if (not reader.fieldnames or not required <= set(reader.fieldnames)
            or len(reader.fieldnames) != len(set(reader.fieldnames))
            or set(reader.fieldnames) - required - {"session_date", "volume", "observed_at"}):
        raise ValueError("Normalized CSV requires start,end,open,high,low,close.")
    bars = []
    for row in reader:
        if len(bars) >= max_rows:
            raise ValueError("Import row limit exceeded.")
        bar = {name: int(row[name]) for name in ("start", "end")}
        bar.update({name: float(row[name]) for name in ("open", "high", "low", "close")})
        if row.get("session_date"):
            bar["session_date"] = row["session_date"]
        if row.get("volume"):
            bar["volume"] = float(row["volume"])
        bars.append(bar)
    return {"bars": bars, "actions": []}


def _import(request, descriptor, received_at, limits):
    contract = request.get("source_contract", {})
    if not isinstance(contract, dict) or any(not isinstance(contract.get(key), str) or not contract[key].strip()
                                             for key in ("origin", "rights", "price_basis")):
        raise ValueError("Imports require explicit origin, rights and price_basis source contract.")
    if contract["price_basis"] != descriptor["price_basis"]:
        raise ValueError("Import source contract and descriptor price basis disagree.")
    descriptor["rights"] = contract["rights"]
    if contract["origin"].startswith(("https://", "http://")):
        descriptor["source_url"] = contract["origin"]
    if "action_coverage" in descriptor:
        descriptor["action_coverage"]["known_at"] = received_at
    if descriptor["price_basis"] not in ("raw", "split_adjusted", "total_return"):
        raise ValueError("Continuous contracts require a separate qualified adapter.")
    if descriptor["data_mode"] not in ("import", "demo"):
        raise ValueError("Imported data cannot be labelled live.")
    if ("import_data" in request) == ("import_csv" in request):
        raise ValueError("Supply exactly one of import_data or import_csv.")
    value = request.get("import_csv", request.get("import_data"))
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("Import must contain finite JSON data.") from exc
    if len(encoded) > limits["max_bytes"]:
        raise ValueError("Import byte limit exceeded.")
    data = _csv_import(value, limits["max_rows"]) if "import_csv" in request else copy.deepcopy(value)
    import_bytes = len(encoded)
    if "import_csv" in request:
        data["actions"] = copy.deepcopy(request.get("import_actions", []))
        try:
            import_bytes += len(json.dumps(data["actions"], allow_nan=False).encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("Import actions must contain finite JSON data.") from exc
    elif "import_actions" in request:
        raise ValueError("Use import_data.actions for JSON imports.")
    if not isinstance(data, dict) or not isinstance(data.get("bars"), list) or not isinstance(data.get("actions", []), list):
        raise ValueError("import_data requires bars and optional actions arrays.")
    if len(data["bars"]) + len(data.get("actions", [])) > limits["max_rows"]:
        raise ValueError("Import row limit exceeded.")
    if import_bytes > limits["max_bytes"]:
        raise ValueError("Import byte limit exceeded.")
    for row in [*data["bars"], *data.get("actions", [])]:
        if not isinstance(row, dict):
            raise ValueError("Each observation must be an object.")
        # Receipt is this ingestion, never a retrospectively asserted old clock.
        row["observed_at"] = received_at
    bars = [row for row in data["bars"] if request["start"] <= _epoch(row.get("start"), "bar start")
            and _epoch(row.get("end"), "bar end") <= request["end"]]
    return {"schema": "market-series.v1", "descriptor": descriptor, "bars": bars,
            "actions": data.get("actions", [])}, import_bytes


def _reference_grid(request, limits):
    grid = request.get("publication_grid")
    if not isinstance(grid, list) or not 1 <= len(grid) <= limits["max_rows"]:
        raise ValueError("FRED requires a bounded explicit publication_grid from the calendar owner.")
    result = {}
    for row in grid:
        if not isinstance(row, dict):
            raise ValueError("Invalid publication grid row.")
        label = row.get("session_date")
        try:
            if date.fromisoformat(label).isoformat() != label:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("Grid session_date requires ISO date.") from exc
        start, end = _epoch(row.get("start"), "grid start"), _epoch(row.get("end"), "grid end")
        if label in result or not request["start"] <= start <= end <= request["end"]:
            raise ValueError("Publication grid contains duplicate dates or out-of-window bounds.")
        result[label] = (start, end)
    return result


def _fred_parse(body, descriptor, grid, received_at, limits):
    try:
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        if reader.fieldnames not in (["observation_date", "DCOILWTICO"], ["DATE", "DCOILWTICO"]):
            raise ValueError("Unexpected FRED CSV schema.")
        bars, seen = [], set()
        for count, row in enumerate(reader, 1):
            if count > limits["max_rows"]:
                raise ValueError("Provider row limit exceeded.")
            label = row.get("observation_date", row.get("DATE"))
            if None in row or date.fromisoformat(label).isoformat() != label:
                raise ValueError("Invalid FRED observation row.")
            if label in seen:
                raise ValueError("Duplicate FRED observation date.")
            seen.add(label)
            if label not in grid:
                continue
            value = row["DCOILWTICO"]
            if value in ("", "."):
                continue
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("Nonfinite FRED observation.")
            start, end = grid[label]
            bars.append({"start": start, "end": end, "session_date": label, "observed_at": received_at,
                         "open": value, "high": value, "low": value, "close": value})
    except (ValueError, UnicodeDecodeError, csv.Error, TypeError, OverflowError) as exc:
        raise ValueError("Malformed FRED CSV or observations.") from exc
    missing = sorted(set(grid) - {bar["session_date"] for bar in bars})
    return {"schema": "market-series.v1", "descriptor": descriptor, "bars": bars, "actions": []}, missing


def fetch_series(request: dict, *, received_at: int | None = None,
                 transport: httpx.BaseTransport | None = None) -> dict:
    """Fetch one explicit series; no persistence, discovery, keys or paid fallback.

    Transport failures return sanitized status and actual attempt metrics. Local
    contract violations raise ValueError before opening a connection. Deadlines
    are cooperative, with per-attempt timeouts, not process termination.
    """
    if not isinstance(request, dict):
        raise ValueError("request must be an object.")
    if set(request) - {"descriptor", "start", "end", "source_contract", "import_data", "import_csv",
                       "import_actions", "opt_in_network", "publication_grid", "limits"}:
        raise ValueError("Unknown provider request fields.")
    descriptor, limits = _descriptor(request), _limits(request)
    start, end = _epoch(request.get("start"), "start"), _epoch(request.get("end"), "end")
    if start >= end:
        raise ValueError("start must precede end.")
    clock = _epoch(int(time.time()) if received_at is None else received_at, "received_at")
    provider = descriptor["provider"]
    if provider not in ("normalized_import", "fred_graph"):
        raise ValueError("Provider is unsupported or unqualified; choose an explicit implemented provider.")
    grid = None
    if provider == "fred_graph":
        if request.get("opt_in_network") is not True:
            raise ValueError("FRED network collection requires opt_in_network=true.")
        required = {"provider_symbol": "DCOILWTICO", "native_currency": "USD", "unit": "USD_per_barrel",
                    "price_basis": "raw", "interval": "reference", "listing_id": None, "data_mode": "live"}
        if any(descriptor.get(key) != value for key, value in required.items()):
            raise ValueError("FRED supports only the USD/barrel DCOILWTICO reference; never a traded listing.")
        descriptor["source_url"] = "https://fred.stlouisfed.org/series/DCOILWTICO"
        grid = _reference_grid(request, limits)
        if any(bounds[1] > clock for bounds in grid.values()):
            raise ValueError("Publication grid cannot contain observations ending after receipt.")
    contract = request.get("source_contract", {})
    if not isinstance(contract, dict):
        raise ValueError("source_contract must be an object.")
    assessment = _assessment(descriptor, contract)
    with http_budget_scope(HTTPPolicy(limits["max_requests"], limits["deadline_seconds"])) as budget:
        meter = HTTPAccounting()
        meter.coverage(provider, instrumented=True)
        result = {"status": "ok", "series": None, "source_assessment": assessment,
                  "metrics": {"import_bytes": 0, "missing_dates": [], "retry_count": 0}}
        if provider == "normalized_import":
            result["series"], result["metrics"]["import_bytes"] = _import(request, descriptor, clock, limits)
        else:
            deadline = time.monotonic() + limits["deadline_seconds"]
            consumed = 0
            with httpx.Client(transport=transport, follow_redirects=False) as client:
                for index in range(limits["max_requests"]):
                    attempt, status, decoded = None, None, 0
                    try:
                        remaining = min(budget.remaining(), deadline - time.monotonic())
                        if remaining <= 0:
                            raise HTTPBudgetExceeded("deadline")
                        budget.supported()
                        budget.admit()
                        attempt = meter.begin(provider, "GET", FRED_URL)
                        result["metrics"]["retry_count"] += int(index > 0)
                        params = {"id": "DCOILWTICO", "cosd": min(grid), "coed": max(grid)}
                        with client.stream("GET", FRED_URL, params=params, timeout=min(remaining, 10)) as response:
                            status = response.status_code
                            chunks = []
                            for chunk in response.iter_bytes(chunk_size=16384):
                                decoded += len(chunk)
                                consumed += len(chunk)
                                if consumed > limits["max_bytes"]:
                                    raise ValueError("Provider byte limit exceeded.")
                                if time.monotonic() >= deadline or budget.remaining() <= 0:
                                    raise HTTPBudgetExceeded("deadline")
                                chunks.append(chunk)
                            body = b"".join(chunks)
                        if time.monotonic() >= deadline or budget.remaining() <= 0:
                            raise HTTPBudgetExceeded("deadline")
                        meter.finish(attempt, status=status, decoded_bytes=decoded)
                        if status in (502, 503, 504) and index + 1 < limits["max_requests"]:
                            continue
                        if status != 200:
                            result.update(status="deferred" if status == 429 else "unavailable", error=f"HTTP {status}")
                            break
                        result["series"], result["metrics"]["missing_dates"] = _fred_parse(
                            body, descriptor, grid, clock, limits)
                        break
                    except (httpx.HTTPError, HTTPBudgetExceeded, ValueError) as exc:
                        if attempt:
                            meter.finish(attempt, status=status, decoded_bytes=decoded, error=True)
                        if isinstance(exc, httpx.TransportError) and index + 1 < limits["max_requests"]:
                            continue
                        result.update(status="unavailable", error=public_error(exc))
                        break
        if result["series"] is not None:
            from .prices import validate_series

            result["series"] = validate_series(result["series"])
        result["http_accounting"] = meter.snapshot(provider)
        result["metrics"]["limits"] = limits
        return result
