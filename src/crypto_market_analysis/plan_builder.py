"""Optional local plan construction and price-free funding constraints.

The preview reserves cash for declared purchases without inventing fills, prices,
rounding refunds, or holdings. It is a conservative funding illustration, not an
account balance. Ledgerly remains authoritative for actual transactions.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

from .plans import DAY, ZERO, _time, decimal_string, validate_config


def _utc_date(value: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("start_date must be an ISO timestamp with explicit UTC timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("start_date must be an ISO timestamp with explicit UTC timezone") from exc
    if result.utcoffset() != timedelta(0) or result.microsecond:
        raise ValueError("start_date must use UTC and whole seconds")
    return result.astimezone(UTC)


def construct_plan(request: dict) -> dict:
    """Expand a bounded recurrence into the existing normalized schema-1 plan.

    Request: explicit asset/quote/source, start_date (UTC ISO timestamp),
    recurrence {frequency, count, month_end_rule}, contribution_amount and
    purchase_amount (decimal strings), plus optional data_mode/fees/execution/
    plans. Monthly clip preserves the original day where possible; skip omits
    months lacking that day. Count is the number of generated occurrences.
    Defaults illustrate regular, half-now split, and fully deferred policies.
    """
    allowed = {"asset", "quote", "source", "data_mode", "start_date", "recurrence",
               "contribution_amount", "purchase_amount", "fees", "execution", "plans"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Unsupported constructor fields")
    start = _utc_date(request.get("start_date"))
    recurrence = request.get("recurrence")
    if not isinstance(recurrence, dict) or set(recurrence) - {"frequency", "count", "month_end_rule"}:
        raise ValueError("recurrence requires frequency and count")
    frequency, count = recurrence.get("frequency"), recurrence.get("count")
    if frequency not in ("daily", "weekly", "monthly"):
        raise ValueError("frequency must be daily, weekly or monthly")
    if type(count) is not int or not 1 <= count <= 181:
        raise ValueError("recurrence.count must be between 1 and 181")
    rule = recurrence.get("month_end_rule")
    if frequency == "monthly" and rule not in ("clip", "skip"):
        raise ValueError("Monthly recurrence requires month_end_rule clip or skip")
    if frequency != "monthly" and rule is not None:
        raise ValueError("month_end_rule applies only to monthly recurrence")
    times = []
    offset = 0
    while len(times) < count:
        try:
            if frequency == "monthly":
                year, month = divmod(start.year * 12 + start.month - 1 + offset, 12)
                month += 1
                last_day = calendar.monthrange(year, month)[1]
                candidate = start.replace(year=year, month=month, day=min(start.day, last_day))
            else:
                candidate = start + timedelta(days=offset * (7 if frequency == "weekly" else 1))
        except (ValueError, OverflowError) as exc:
            raise ValueError("Recurrence is outside the supported UTC range") from exc
        if candidate - start > timedelta(days=180):
            raise ValueError("Expanded recurrence must fit within 180 days")
        if frequency != "monthly" or rule == "clip" or candidate.day == start.day:
            times.append(int(candidate.timestamp()))
        offset += 1
    config = {"schema_version": 1, "asset": request.get("asset"), "quote": request.get("quote"),
              "source": request.get("source"), "data_mode": request.get("data_mode", "live"),
              "start_time": times[0],
              "contributions": [{"time": t, "amount": request.get("contribution_amount")} for t in times],
              "schedule": [{"time": t, "amount": request.get("purchase_amount")} for t in times],
              "fees": request.get("fees", {}), "execution": request.get("execution", {}),
              "plans": request.get("plans", {"split": {"initial_fraction": "0.5"},
                                              "deferred": {"fraction": "1"}})}
    return validate_config(config)


def policies_equivalent(config: dict) -> bool:
    """Identify split and deferred policies with the same declared allocations."""
    split, deferred = config["plans"]["split"], config["plans"]["deferred"]
    common = ("max_wait_days", "fallback", "max_deferred_reserve_quote")
    return (all(split[key] == deferred[key] for key in common)
            and Decimal(split["initial_fraction"]) == 1 - Decimal(deferred["fraction"])
            and split["levels"] == [{"decline": deferred["decline"], "fraction": deferred["fraction"]}])


def allocation_specs(config: dict, family: str, amount: Decimal) -> list[dict]:
    """Describe notional slices in the simulator's allocation order."""
    if family == "regular":
        return [{"requested_notional": amount, "decline": None}]
    policy = config["plans"][family]
    initial = (Decimal(policy["initial_fraction"]) if family == "split"
               else 1 - Decimal(policy["fraction"]))
    levels = policy["levels"] if family == "split" else [policy]
    specs = [{"requested_notional": amount * initial, "decline": None}]
    specs.extend({"requested_notional": amount * Decimal(row["fraction"]),
                  "decline": row["decline"]} for row in levels)
    return [row for row in specs if row["requested_notional"] > 0]


def required_cash(config: dict, notional: Decimal) -> Decimal:
    fees = config["fees"]
    return (notional * (1 + Decimal(fees["rate"])) + Decimal(fees["fixed"])
            if fees["asset"] == "quote" and notional > 0 else notional)


def _preview_family(config: dict, as_of: int, family: str) -> dict:
    """Reserve declared cash, with no price-dependent fill or refund assumptions."""
    cash, deposits = ZERO, ZERO
    commitments, rows = [], []
    events = {as_of}
    contributions, schedules = {}, {}
    policy = config["plans"].get(family)
    for row in config["contributions"]:
        contributions.setdefault(row["time"], []).append(Decimal(row["amount"]))
        events.add(row["time"])
    for row in config["schedule"]:
        schedules.setdefault(row["time"], []).append(Decimal(row["amount"]))
        events.add(row["time"])
        if policy:
            events.add(row["time"] + policy["max_wait_days"] * DAY)
    current = None
    for time in sorted(events):
        for amount in contributions.get(time, []):
            cash += amount
            deposits += amount
        for commitment in commitments:
            if commitment["locked"] and time >= commitment["deadline"]:
                commitment["locked"] = False
                if commitment["fallback"] == "reserve":
                    cash += commitment["cash"]
                    commitment["cash"] = ZERO
        for amount in schedules.get(time, []):
            allocations = []
            available = cash
            for spec in allocation_specs(config, family, amount):
                needed = required_cash(config, spec["requested_notional"])
                allocated = min(cash, needed)
                cash -= allocated
                locked = allocated if spec["decline"] is not None else ZERO
                if policy and locked and policy["max_deferred_reserve_quote"] is not None:
                    already = sum((c["cash"] for c in commitments if c["locked"]), ZERO)
                    locked = min(locked, max(ZERO, Decimal(policy["max_deferred_reserve_quote"]) - already))
                deadline = time + policy["max_wait_days"] * DAY if policy and spec["decline"] else None
                if locked:
                    commitments.append({"cash": locked, "locked": True, "deadline": deadline,
                                        "fallback": policy["fallback"]})
                overflow = allocated - locked if spec["decline"] else ZERO
                immediate = allocated - locked
                if immediate:
                    commitments.append({"cash": immediate, "locked": False, "deadline": None,
                                        "fallback": None})
                allocations.append({"requested_notional": decimal_string(spec["requested_notional"]),
                                    "decline": spec["decline"], "required_cash": decimal_string(needed),
                                    "allocated_cash": decimal_string(allocated), "funding_shortfall": allocated < needed,
                                    "locked_cash": decimal_string(locked),
                                    "cap_overflow_immediate_cash": decimal_string(overflow),
                                    "deadline": deadline, "fallback": policy["fallback"] if deadline else None})
            rows.append({"time": time, "phase": "future" if time > as_of else "current" if time == as_of else "past",
                         "requested_notional": decimal_string(amount),
                         "cash_available_before": decimal_string(available), "allocations": allocations,
                         "funding_shortfall": any(row["funding_shortfall"] for row in allocations),
                         "free_cash_after": decimal_string(cash)})
        if time == as_of:
            current = {"contributions_available": decimal_string(deposits), "free_cash": decimal_string(cash),
                       "locked_cash": decimal_string(sum((c["cash"] for c in commitments if c["locked"]), ZERO)),
                       "reserved_immediate_cash": decimal_string(sum((c["cash"] for c in commitments if not c["locked"]), ZERO))}
    return {"current": current, "tranches": rows,
            "next_tranche": next((row for row in rows if row["time"] >= as_of), None)}


def preview_plan_funding(config: dict, as_of: int) -> dict:
    """Preview declared cash constraints without market prices or actual holdings.

    Deposits become available at their timestamps. Cash assigned to an immediate
    purchase remains committed; target cash releases only on reserve fallback.
    Thus this preview cannot claim execution, remaining account cash or rounding
    refunds. The historical simulator supplies those separate hypothetical paths.
    """
    config = validate_config(config)
    as_of = _time(as_of, "as_of")
    with localcontext() as ctx:
        ctx.prec = 180
        return {"schema_version": 1, "kind": "price_free_funding_preview", "as_of": as_of,
                "market": {key: config[key] for key in ("asset", "quote", "source", "data_mode")},
                "fees": config["fees"], "policies_equivalent": policies_equivalent(config),
                "families": {family: _preview_family(config, as_of, family)
                             for family in ("regular", "split", "deferred")},
                "future_contributions": [{**row} for row in config["contributions"] if row["time"] > as_of],
                "assumptions": ["Declared quote notional excludes quote fees; each positive slice incurs its own fixed fee.",
                    "No borrowing: deposits are available only at their declared timestamps.",
                    "Price-free cash commitments assume no fills or rounding refunds; this is not an account balance.",
                    "A deferred reserve cap can create an additional immediate purchase and additional fixed fee; allocated cash is unchanged and achievable notional can fall.",
                    "Base-asset fees reduce received units; their quantity cannot be estimated without a price.",
                    "Ledgerly remains authoritative for actual transactions and holdings."]}
