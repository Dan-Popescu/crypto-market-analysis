"""Read-only next-tranche review from one explicitly pinned captured input set."""

from __future__ import annotations

from decimal import Decimal, localcontext

from .context import _eligible_candles, _timestamp, market_context
from .evidence import CapturedInputs
from .plans import (
    DAY,
    compare_plans,
    decimal_string,
    next_activation_funding,
    preview_plan_funding,
    validate_config,
)
from .purchase_windows import analyze_windows

_FAMILIES = ("regular", "split", "deferred")


def _next_tranche(config, candles, as_of, current, anchor):
    tranche = next_activation_funding(config, candles, as_of, current_simulation=current)
    if tranche is None:
        return None
    time = tranche["time"]
    within_tolerance = (time > as_of or (anchor["age_seconds"] is not None
                        and anchor["age_seconds"] <= config["execution"]["max_price_age_seconds"]))
    for family in tranche["families"].values():
        for allocation in family["allocations"]:
            decline = allocation["decline"]
            target = (Decimal(anchor["price"]) * (1 - Decimal(decline))
                      if decline is not None and anchor["status"] == "fresh" and within_tolerance else None)
            allocation.update({
                "target_price": decimal_string(target) if target is not None else None,
                "target_interpretation": ("indicative_current_anchor_not_future_execution_anchor" if time > as_of
                    else "current_observed_anchor_hypothetical_policy") if target is not None
                    else "suppressed_activation_anchor_outside_plan_tolerance"
                    if decline and not within_tolerance and anchor["status"] == "fresh"
                    else "suppressed_stale_or_missing_anchor" if decline else "immediate",
                "anchor_time": anchor["time"] if target is not None else None,
                "activation_anchor_status": ("unknown_until_future_activation" if time > as_of else "available"
                    if anchor["status"] == "fresh" and within_tolerance else "unavailable"),
                "allocation_status": ("future_policy_preview" if time > as_of else "requires_activation_anchor"
                    if decline and target is None else "hypothetical_current_activation")})
    return tranche


def _historical_outcomes(config, as_of, current, reference):
    outcomes = {}
    for days in (90, 180):
        time = config["start_time"] + days * DAY
        outcome = {"time": time, "input_reference": reference}
        if time > as_of:
            outcomes[str(days)] = {**outcome, "status": "pending", "reason": "future_horizon"}
            continue
        terminals = {family: current["plans"][family]["terminals"][str(days)] for family in _FAMILIES}
        baseline = terminals["regular"]
        paths = {family: terminal["execution_coverage"] for family, terminal in terminals.items()}
        path_complete = all(path["qualified"] for path in paths.values())
        paired = (all(row["status"] == "complete" for row in terminals.values())
                  and len({(row["price"], row["contributions"]) for row in terminals.values()}) == 1)
        comparisons = {}
        if paired and path_complete:
            for family in ("split", "deferred"):
                terminal = terminals[family]
                comparisons[family] = {"terminal_wealth_difference": decimal_string(Decimal(terminal["total_wealth"]) - Decimal(baseline["total_wealth"])),
                    "reserve_difference": decimal_string(Decimal(terminal["reserve"]) - Decimal(baseline["reserve"])),
                    "quantity_difference": decimal_string(Decimal(terminal["net_quantity"]) - Decimal(baseline["net_quantity"]))}
        outcomes[str(days)] = {**outcome, "status": "complete" if paired and path_complete else "insufficient",
            "reason": None if paired and path_complete else "execution_path_incomplete" if paired else "terminal_price_unavailable",
            "plans": terminals, "comparisons": comparisons,
            "path_coverage": {"complete": path_complete, "execution_paths": paths,
                              "reasons": sorted({reason for path in paths.values() for reason in path["reasons"]})},
            "interpretation": "Retrospective configured-cost simulation; not actual holdings, prospective evidence or demonstrated performance."}
    return outcomes


def build_accumulation_brief(captured: CapturedInputs, plan_config: dict | None = None) -> dict:
    """Combine existing pure calculations using only this pinned captured set.

    No storage access, collection, issuance, policy selection or order execution.
    The optional declared plan must match source, asset, native quote and mode.
    """
    _timestamp(captured.as_of, "as_of")
    if not isinstance(captured.source, str) or not captured.source or captured.quote not in ("EUR", "USD"):
        raise ValueError("The brief requires an explicit source and native EUR or USD quote")
    if captured.asset not in ("BTC", "ETH") or captured.data_mode not in ("live", "import", "demo"):
        raise ValueError("Unsupported captured market identity")
    market = {key: getattr(captured, key) for key in ("asset", "quote", "source", "data_mode")}
    candles = list(captured.candles)
    if any(any(getattr(candle, key) != value for key, value in market.items()) for candle in candles):
        raise ValueError("Captured candles must match the pinned market identity")
    cadence = captured.configuration.get("market_interval", 3600)
    if type(cadence) is not int or not 1 <= cadence <= 86400:
        raise ValueError("market_interval must be a positive bounded integer")
    reference = captured.reference
    eligible, _ = _eligible_candles(candles, captured.as_of)
    latest = eligible[-1] if eligible else None
    age = captured.as_of - latest.close_time if latest else None
    stale = age is not None and age >= cadence * 2
    anchor = {"price": decimal_string(Decimal(str(latest.close))) if latest else None,
              "time": latest.close_time if latest else None,
              "observed_at": latest.observed_at if latest else None, "age_seconds": age,
              "stale": stale, "status": "unavailable" if latest is None else "stale" if stale else "fresh",
              "max_age_seconds": cadence * 2, "kind": "completed_hourly_close",
              "current_price_interpretation_available": latest is not None and not stale}
    context = {**market_context(candles, captured.as_of), "market": market, "input_reference": reference}
    windows = {**analyze_windows(candles, captured.as_of, max_price_age=cadence * 2),
               "market": market, "input_reference": reference}
    policy = {"status": "not_supplied", "input_reference": reference}
    outcomes = {str(days): {"status": "unavailable", "reason": "plan_not_supplied", "input_reference": reference}
                for days in (90, 180)}
    if plan_config is not None:
        config = validate_config(plan_config)
        if any(config[key] != value for key, value in market.items()):
            raise ValueError("The plan must match the captured source, asset, native quote and data mode")
        current = (compare_plans(config, candles, captured.as_of) if captured.as_of >= config["start_time"] else None)
        if current:
            current["input_reference"] = reference
        with localcontext() as ctx:
            ctx.prec = 180
            next_tranche = _next_tranche(config, candles, captured.as_of, current, anchor)
            outcomes = _historical_outcomes(config, captured.as_of, current, reference)
        policy = {"status": "declared", "input_reference": reference, "config": config,
                  "funding_preview": preview_plan_funding(config, captured.as_of),
                  "next_tranche": next_tranche, "current_simulation": current,
                  "pending_targets": {family: [{**row,
                      "fallback": config["plans"][family]["fallback"] if row["deadline"] is not None else "buy_at_deadline",
                      "interpretation": "Frozen hypothetical activation target; not a current-price purchase instruction."}
                      for row in current["plans"][family]["waiting_targets"]] if current else [] for family in _FAMILIES},
                  "interpretation": "Caller-declared hypothetical rules; no policy recommendation or actual account balance."}
    return {"schema_version": 1, "kind": "accumulation_brief", "as_of": captured.as_of,
            "market": market, "input_reference": reference, "input_truncation": dict(captured.truncation),
            "current_observed": {"market": market, "as_of": captured.as_of, "input_reference": reference,
                                 "anchor": anchor, "context": context, "purchase_windows": windows},
            "hypothetical_policy": policy, "historical_outcomes": outcomes,
            "limitations": ["Experimental historical frequencies do not select a purchase policy or establish predictive edge.",
                "Future tranche targets use indicative current-anchor examples; the actual activation anchor is unknown.",
                "Stale or missing anchors suppress current price-based targets and conditional frequencies.",
                "Historical plan paths use configured costs, not measured fills; future 90/180-day outcomes remain pending.",
                "A locked-target cash cap is not a protected cash floor.",
                "EUR and USD remain separate native markets; no conversion or cross-venue averaging.",
                "No actual holdings or accounts are required; Ledgerly remains authoritative for transactions and holdings."]}
