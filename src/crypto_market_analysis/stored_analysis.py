"""Read-only bridges from bounded stored evidence to the pure local analyzers.

Selectors are explicit. Reads never collect, qualify a provider, issue a brief,
or write a journal. Unknown clocks remain evidence without becoming endpoints.
"""

from __future__ import annotations

from copy import deepcopy

from .derivatives_context import analyze_derivatives_context
from .evidence import content_hash
from .quote_context import analyze_quote_context
from .storage import Store
from .venue_analysis import analyze_purchase_costs, analyze_venue_disagreement


def _request(request: dict, allowed: set[str], *, asset: bool = False) -> dict:
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Unsupported stored analysis request fields.")
    if request.get("data_mode") not in ("live", "import", "demo"):
        raise ValueError("Stored analysis requires explicit data_mode live, import or demo.")
    if not isinstance(request.get("quote"), str) or not request["quote"]:
        raise ValueError("Stored analysis requires an explicit native quote.")
    if asset and request.get("asset") not in ("BTC", "ETH"):
        raise ValueError("Stored analysis requires BTC or ETH asset.")
    return deepcopy(request)


def _evidence(snapshot: dict, analytical_request: dict, unknown: list[dict] | None = None) -> dict:
    records = snapshot["records"]
    return {
        "selection": snapshot["selection"], "watermark": snapshot["watermark"],
        "input_references": [row["input_reference"] for row in records],
        "input_hash": content_hash({"request": analytical_request, "records": records}),
        "hash_scope": "canonical analytical request and exact consumed stored records",
        "consumed_records": records, "truncation": snapshot["truncation"],
        "unknown_inputs": unknown or [],
        "read_only": True,
    }


def build_stored_derivatives_inputs(store: Store, request: dict, as_of: int) -> dict:
    """Build the pure request from every known vintage in a bounded 48h window.

    Explicit request keys: asset, source, native quote, data_mode and optional
    policy. The 48h window includes the full maximum one-day point age before
    the 24h endpoint. Required rows exceeding 5000 fail before analysis.
    """
    request = _request(request, {"asset", "source", "quote", "data_mode", "policy"}, asset=True)
    source = request.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("Stored derivatives require one explicit source.")
    snapshot = store.stored_analysis_snapshot(
        "derivatives", as_of=as_of, mode=request["data_mode"], sources=[source],
        quote=request["quote"], asset=request["asset"])
    analytical = {"schema_version": 1, **request,
                  "observations": [row["payload"] for row in snapshot["records"]]}
    return {"request": analytical, "stored_inputs": _evidence(snapshot, analytical)}


def analyze_stored_derivatives(store: Store, request: dict, as_of: int) -> dict:
    """Analyze stored native derivatives without fetching or rewriting history."""
    built = build_stored_derivatives_inputs(store, request, as_of)
    return {**analyze_derivatives_context(built["request"], as_of), "stored_inputs": built["stored_inputs"]}


def build_stored_quote_inputs(store: Store, request: dict, as_of: int) -> dict:
    """Build explicit native quote context, retaining unusable records as unknown.

    sources is a bounded explicit list, including the desired FX source when
    wanted. quote selects only stablecoin prices in that native currency. FX
    references remain separately identified EUR/USD or USD/EUR. More than one
    selected FX reference is ambiguous and requires narrower source selection.
    """
    request = _request(request, {"asset", "sources", "quote", "data_mode", "max_age_seconds", "max_endpoint_skew_seconds"})
    if request.get("asset") is not None and request["asset"] not in ("BTC", "ETH"):
        raise ValueError("Optional quote context asset must be BTC or ETH.")
    if request["quote"] not in ("EUR", "USD"):
        raise ValueError("Stored quote context requires native EUR or USD.")
    snapshot = store.stored_analysis_snapshot(
        "quotes", as_of=as_of, mode=request["data_mode"], sources=request.get("sources"), quote=request["quote"])
    analytical = {key: value for key, value in request.items() if key not in ("asset", "sources", "quote")}
    analytical["stablecoin_prices"] = []
    unknown = []
    fx_records = [record for record in snapshot["records"] if record["kind"] == "fx_reference"]
    if len(fx_records) > 1:
        raise ValueError("Select one FX source and orientation; stored FX references are never pooled.")
    for record in snapshot["records"]:
        row = record["payload"]
        reason = None
        detail = None
        if type(row.get("event_time")) is not int:
            reason = "unknown_timestamp"
        else:
            # Reuse strict validation without inventing units, types or clocks.
            single = {**analytical, "stablecoin_prices": []}
            single["fx_reference" if record["kind"] == "fx_reference" else "stablecoin_prices"] = (
                row if record["kind"] == "fx_reference" else [row])
            try:
                analyze_quote_context(single, as_of)
            except ValueError as exc:
                reason, detail = "invalid_quote_evidence", str(exc)
        if reason:
            unknown.append({**record, "status": reason, **({"reason": detail} if detail else {})})
        elif record["kind"] == "fx_reference":
            analytical["fx_reference"] = row
        else:
            analytical["stablecoin_prices"].append(row)
    return {"request": analytical, "stored_inputs": _evidence(snapshot, analytical, unknown)}


def analyze_stored_quotes(store: Store, request: dict, as_of: int) -> dict:
    """Describe stored FX/basis, exposing date-only rates without a fake clock."""
    built = build_stored_quote_inputs(store, request, as_of)
    result = analyze_quote_context(built["request"], as_of)
    for record in built["stored_inputs"]["unknown_inputs"]:
        item = {"status": record["status"], "observation": record["payload"],
                "input_reference": record["input_reference"]}
        if record["kind"] == "fx_reference":
            result["fx_reference"] = {**result["fx_reference"], **item, "executable": False}
        else:
            result["stablecoin_basis"].append({**item, "basis_percent": None, "usd_per_token": None})
    # EUR basis must explain why the visibly preserved reference cannot be used.
    if result["fx_reference"]["status"] != "missing":
        for row in result["stablecoin_basis"]:
            if row["status"] == "fx_reference_missing":
                row["status"] = "fx_reference_" + result["fx_reference"]["status"]
    return {**result, "stored_inputs": built["stored_inputs"]}


def analyze_stored_venues(store: Store, request: dict, as_of: int) -> dict:
    """Use one stored book snapshot for native disagreement or purchase costs.

    Common keys: analysis (disagreement or purchase_costs), asset, quote,
    data_mode, sources, optional max_age_seconds/max_skew_seconds. Disagreement
    also accepts price_basis/outlier_threshold_bps; costs accepts budget and
    explicit assumptions_by_source/product_by_source/purchase_eligibility_by_source.
    Missing fee, product and eligibility evidence stays unknown.
    """
    # Import the book boundary only here; quote/derivative reads need no adapters.
    from .book_snapshots import disagreement_request, purchase_cost_request

    common = {"analysis", "asset", "quote", "data_mode", "sources", "max_age_seconds", "max_skew_seconds"}
    disagreement = {"price_basis", "outlier_threshold_bps"}
    purchase = {"budget", "assumptions_by_source", "product_by_source", "purchase_eligibility_by_source"}
    request = _request(request, common | disagreement | purchase, asset=True)
    analysis = request.pop("analysis", None)
    if analysis not in ("disagreement", "purchase_costs"):
        raise ValueError("Stored venue analysis requires disagreement or purchase_costs.")
    if set(request) - (common | (disagreement if analysis == "disagreement" else purchase)):
        raise ValueError("Stored venue fields must match the requested analysis.")
    if "sources" not in request or (analysis == "purchase_costs" and "budget" not in request):
        raise ValueError("Stored venue analysis requires explicit sources and purchase costs require a budget.")
    constructor, analyzer = ((disagreement_request, analyze_venue_disagreement) if analysis == "disagreement"
                             else (purchase_cost_request, analyze_purchase_costs))
    analytical = constructor(store, as_of=as_of, **request)
    result = analyzer(analytical, as_of)
    selection = analytical["book_selection"]
    coverage = [{"source": row["source"], **row["coverage"]} for row in selection["selected"]]
    result["stored_inputs"] = {
        "selection": {key: request[key] for key in ("asset", "quote", "data_mode", "sources")},
        "watermark": selection["watermark"],
        "input_hash": content_hash(analytical), "hash_scope": "canonical analytical request and book selection",
        "input_references": [row["input_reference"] for row in selection["selected"]],
        "consumed_records": analytical["snapshots"], "unknown_inputs": selection["excluded"],
        "book_selection": selection,
        "truncation": {"truncated": any(row.get("asks_truncated") or row.get("bids_truncated") for row in coverage),
                       "selection_truncated": False, "book_coverage": coverage}, "read_only": True,
    }
    return result
