"""Read-only next-tranche review from one explicitly pinned captured input set."""

from __future__ import annotations

from decimal import Decimal, localcontext

from .context import _eligible_candles, _timestamp, market_context
from .evidence import CapturedInputs
from .plan_builder import allocation_specs, preview_plan_funding, required_cash
from .plans import DAY, ZERO, _market_bars, compare_plans, decimal_string, validate_config
from .purchase_windows import analyze_windows

_FAMILIES = ("regular", "split", "deferred")


def _next_tranche(config, candles, as_of, current, anchor):
    upcoming = [row for row in config["schedule"] if row["time"] >= as_of]
    if not upcoming:
        return None
    time = upcoming[0]["time"]
    amount = sum((Decimal(row["amount"]) for row in upcoming if row["time"] == time), ZERO)
    # At an exact activation cutoff, preview cash before that activation. This
    # avoids counting the same simulator allocation twice.
    before = current
    if time == as_of:
        before = (compare_plans(config, candles, as_of - 1) if as_of > config["start_time"] else None)
    cash_cutoff = as_of - 1 if time == as_of else as_of
    available_deposits = sum((Decimal(row["amount"]) for row in config["contributions"]
                              if row["time"] <= as_of and row["time"] > cash_cutoff), ZERO)
    if before is None:
        available_deposits = sum((Decimal(row["amount"]) for row in config["contributions"]
                                  if row["time"] <= as_of), ZERO)
    future = sum((Decimal(row["amount"]) for row in config["contributions"]
                  if as_of < row["time"] <= time), ZERO)
    families = {}
    for family in _FAMILIES:
        simulated = before["plans"][family] if before else None
        current_cash = (Decimal(simulated["unallocated_reserve"]) if simulated else ZERO) + available_deposits
        current_left, future_left = current_cash, future
        policy = config["plans"].get(family)
        locked = Decimal(simulated["locked_reserve"]) if simulated else ZERO
        allocations = []
        # Preserve separate same-timestamp orders: their fixed fees do not merge.
        for tranche in (row for row in upcoming if row["time"] == time):
            for spec in allocation_specs(config, family, Decimal(tranche["amount"])):
                notional, decline = spec["requested_notional"], spec["decline"]
                needed = required_cash(config, notional)
                funded_now = min(current_left, needed)
                current_left -= funded_now
                funded_future = min(future_left, needed - funded_now)
                future_left -= funded_future
                allocated = funded_now + funded_future
                target_cash = allocated if decline is not None else ZERO
                if target_cash and policy["max_deferred_reserve_quote"] is not None:
                    target_cash = min(target_cash, max(ZERO, Decimal(policy["max_deferred_reserve_quote"]) - locked))
                locked += target_cash
                within_tolerance = (time > as_of or (anchor["age_seconds"] is not None
                                     and anchor["age_seconds"] <= config["execution"]["max_price_age_seconds"]))
                target = (Decimal(anchor["price"]) * (1 - Decimal(decline))
                          if decline is not None and anchor["status"] == "fresh" and within_tolerance else None)
                allocations.append({"requested_notional": decimal_string(notional), "decline": decline,
                    "target_price": decimal_string(target) if target is not None else None,
                    "target_interpretation": ("indicative_current_anchor_not_future_execution_anchor" if time > as_of
                                               else "current_observed_anchor_hypothetical_policy")
                    if target is not None else "suppressed_activation_anchor_outside_plan_tolerance"
                    if decline and not within_tolerance and anchor["status"] == "fresh"
                    else "suppressed_stale_or_missing_anchor" if decline else "immediate",
                    "anchor_time": anchor["time"] if target is not None else None,
                    "activation_anchor_status": ("unknown_until_future_activation" if time > as_of
                        else "available" if anchor["status"] == "fresh" and within_tolerance else "unavailable"),
                    "allocation_status": ("future_policy_preview" if time > as_of else "requires_activation_anchor"
                        if decline and target is None else "hypothetical_current_activation"),
                    "deadline": time + policy["max_wait_days"] * DAY if decline else None,
                    "fallback": policy["fallback"] if decline else None,
                    "required_cash": decimal_string(needed), "allocated_cash": decimal_string(allocated),
                    "funded_from_current_cash": decimal_string(funded_now),
                    "funded_from_future_contributions": decimal_string(funded_future),
                    "funding_shortfall": allocated < needed, "locked_cash": decimal_string(target_cash),
                    "cap_overflow_immediate_cash": decimal_string(allocated - target_cash) if decline else "0"})
        families[family] = {"current_cash_available": decimal_string(current_cash),
            "future_contributions_before_activation": decimal_string(future),
            "immediate_notional": decimal_string(sum((Decimal(row["requested_notional"]) for row in allocations if row["decline"] is None), ZERO)),
            "deferred_notional": decimal_string(sum((Decimal(row["requested_notional"]) for row in allocations if row["decline"] is not None), ZERO)),
            "allocations": allocations, "free_cash_after": decimal_string(current_left + future_left),
            "locked_cash_after": decimal_string(locked),
            "funding_shortfall": any(row["funding_shortfall"] for row in allocations)}
    return {"time": time, "requested_notional": decimal_string(amount), "families": families,
            "future_contributions_before_activation": decimal_string(future),
            "funding_basis": "Cash-capacity illustration using hypothetical current free reserve plus deposits due by activation; activation still requires eligible prices. No borrowing, future fills, releases or rounding refunds assumed.",
            "future_anchor_unknown": time > as_of}


def _historical_outcomes(config, candles, as_of, current, reference):
    outcomes = {}
    bars = _market_bars(config, candles, as_of, "retrospective")
    duration = 3600 if config["execution"]["timeframe"] == "1h" else DAY
    for days in (90, 180):
        time = config["start_time"] + days * DAY
        outcome = {"time": time, "input_reference": reference}
        if time > as_of:
            outcomes[str(days)] = {**outcome, "status": "pending", "reason": "future_horizon"}
            continue
        terminals = {family: current["plans"][family]["terminals"][str(days)] for family in _FAMILIES}
        baseline = terminals["regular"]
        first_open = (config["start_time"] + duration - 1) // duration * duration
        expected = set(range(first_open, time - duration + 1, duration))
        observed = {bar.open_time for bar in bars if first_open <= bar.open_time and bar.close_time <= time}
        activations = [row["time"] for row in config["schedule"] if row["time"] < time]
        anchors_available = all(any(bar.close_time <= activation
            and activation - bar.close_time <= config["execution"]["max_price_age_seconds"]
            for bar in bars) for activation in activations)
        path_complete = bool(expected) and expected <= observed and anchors_available
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
            "path_coverage": {"required_bars": len(expected), "observed_bars": len(expected & observed),
                              "activation_anchors_available": anchors_available, "complete": path_complete},
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
            outcomes = _historical_outcomes(config, candles, captured.as_of, current, reference)
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
