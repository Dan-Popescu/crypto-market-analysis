"""Offline native-quote snapshot diagnostics; never an execution or buy signal.

Callers supply actual ask levels, not stored aggregate depth bands. Fees, product
and minimum rules are explicit assumptions, not inferred exchange defaults. All
calculations are hypothetical at the supplied snapshot; even sufficient captured
depth cannot guarantee a fill. No collection, persistence or orders occur here.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from copy import deepcopy
from decimal import ROUND_CEILING, Context, Decimal, localcontext

from .reporting import bounded_object

RULE_VERSION = "offline-venue-snapshots-v1"
_MAX_TIME = 253402300799
_ZERO = Decimal(0)
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?\Z")
_COST_FIELDS = (
    "fee_rate", "fee_step", "fixed_fee_quote", "payment_cost_quote",
    "withdrawal_cost_quote", "withdrawal_cost_base", "quantity_step",
    "price_step", "min_quantity", "min_notional",
)
_LIMITATIONS = [
    "Caller-supplied snapshots and assumptions; no live access or execution was measured.",
    "EUR and USD are independent native markets; stablecoins are never treated as USD.",
    "No orders, cheapest-platform ranking, fair-value index or purchase recommendation.",
]


def _decimal(value, name: str, *, positive: bool = False) -> Decimal:
    # Strings and integers preserve exact input; binary floating point is rejected.
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not _DECIMAL.fullmatch(str(value)):
        raise ValueError(f"{name} requires a nonnegative decimal string with at most 18 integer/fraction digits.")
    number = Decimal(value)
    if positive and not number:
        raise ValueError(f"{name} must be positive.")
    return number


def _text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _ratio(value: Decimal) -> str:
    return _text(value.quantize(Decimal("0.000000000000000001")))


def _label(value, name: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or value != value.strip() or not value.isprintable()):
        raise ValueError(f"{name} requires a printable nonempty string of at most 256 characters.")
    return value


def _integer(value, name: str, maximum: int = _MAX_TIME) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} requires an integer from 0 to {maximum}.")
    return value


def _common(request: dict, as_of: int) -> tuple[list[str], dict[str, dict]]:
    bounded_object(request, "venue analysis request")
    _integer(as_of, "as_of")
    if request.get("asset") not in ("BTC", "ETH") or request.get("quote") not in ("EUR", "USD"):
        raise ValueError("asset must be BTC/ETH and quote must be native EUR/USD.")
    if request.get("data_mode") not in ("live", "import", "demo"):
        raise ValueError("data_mode must be explicitly live, import or demo.")
    _integer(request.get("max_age_seconds"), "max_age_seconds", 86400)
    _integer(request.get("max_skew_seconds"), "max_skew_seconds", 86400)
    sources = request.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise ValueError("sources requires 1 to 32 unique source identifiers.")
    for source in sources:
        _label(source, "source")
    if len(set(sources)) != len(sources):
        raise ValueError("sources requires unique identifiers.")
    snapshots = request.get("snapshots")
    if not isinstance(snapshots, list) or len(snapshots) > len(sources):
        raise ValueError("snapshots must be a list with at most one record per requested source.")
    selected = {}
    for row in snapshots:
        if not isinstance(row, dict) or row.get("source") not in sources:
            raise ValueError("Each snapshot requires a requested source identifier.")
        source = row["source"]
        if source in selected:
            raise ValueError("Duplicate source snapshots are ambiguous; select one vintage explicitly.")
        if any(row.get(key) != request[key] for key in ("asset", "quote", "data_mode")):
            raise ValueError("Every snapshot must match the explicit asset, native quote and data_mode.")
        _label(row.get("input_reference"), "snapshot input_reference")
        _integer(row.get("event_time"), "event_time")
        _integer(row.get("captured_at"), "captured_at")
        selected[source] = row
    return sources, selected


def _timing(row: dict, request: dict, as_of: int) -> dict:
    event_age, capture_age = as_of - row["event_time"], as_of - row["captured_at"]
    skew = row["event_time"] > row["captured_at"] or min(event_age, capture_age) < 0
    stale = max(event_age, capture_age) > request["max_age_seconds"]
    return {
        "event_time": row["event_time"], "captured_at": row["captured_at"],
        "event_age_seconds": event_age, "price_age_seconds": event_age, "capture_age_seconds": capture_age,
        "capture_delay_seconds": row["captured_at"] - row["event_time"],
        "clock_skew": skew, "stale": stale,
    }


def _alignment(rows: list[dict], request: dict) -> dict:
    timings = [row["timing"] for row in rows if row.get("timing") is not None]
    def skew(key):
        values = [row[key] for row in timings]
        return max(values) - min(values) if values else None
    event, capture = skew("event_time"), skew("captured_at")
    return {
        "event_skew_seconds": event, "capture_skew_seconds": capture,
        "max_skew_seconds": request["max_skew_seconds"],
        "aligned": bool(timings) and max(event, capture) <= request["max_skew_seconds"]
        and not any(row["clock_skew"] for row in timings),
    }


def _base_result(request: dict, as_of: int, kind: str, rows: list[dict]) -> dict:
    return {
        "kind": kind, "rule_version": RULE_VERSION, "as_of": as_of,
        "asset": request["asset"], "quote": request["quote"], "data_mode": request["data_mode"],
        "rows": rows, "timing": _alignment(rows, request),
        "coverage": {
            "requested_sources": request["sources"].copy(),
            "present_sources": [row["source"] for row in rows if row["status"] != "missing"],
            "missing_sources": [row["source"] for row in rows if row["status"] == "missing"],
        },
        "limitations": _LIMITATIONS.copy(),
    }


def _row(source: str, snapshot: dict | None, request: dict, as_of: int) -> dict:
    if snapshot is None:
        return {"source": source, "status": "missing", "reasons": ["missing_snapshot"], "timing": None}
    timing = _timing(snapshot, request, as_of)
    reasons = []
    if timing["clock_skew"]:
        reasons.append("clock_skew")
    if timing["stale"]:
        reasons.append("stale_snapshot")
    return {
        "source": source, "status": "unsupported" if reasons else "supported",
        "input_reference": snapshot["input_reference"], "timing": timing, "reasons": reasons,
    }


def _assumptions(snapshot: dict) -> tuple[dict | None, list[str]]:
    values = snapshot.get("assumptions")
    if values is None:
        return None, ["unknown_cost_and_product_rules"]
    if not isinstance(values, dict):
        raise ValueError("assumptions must be an object or null.")
    reasons = [f"unknown_{field}" for field in (*_COST_FIELDS, "fee_currency", "input_reference", "fee_tier")
               if values.get(field) is None]
    parsed = {field: _decimal(values[field], field) for field in _COST_FIELDS if values.get(field) is not None}
    for field in ("quantity_step", "price_step"):
        if field in parsed and not parsed[field]:
            raise ValueError(f"{field} must be positive; use null for an unknown rule.")
    if parsed.get("fee_rate", _ZERO) >= 1:
        raise ValueError("fee_rate must be less than one.")
    if values.get("fee_currency") is not None and values["fee_currency"] not in ("quote", "base"):
        raise ValueError("fee_currency must be quote or base.")
    for field in ("input_reference", "fee_tier"):
        if values.get(field) is not None:
            _label(values[field], f"assumptions {field}")
    return ({**parsed, "fee_currency": values["fee_currency"]} if not reasons else None), reasons


def _asks(snapshot: dict) -> list[tuple[Decimal, Decimal]]:
    levels = snapshot.get("asks")
    if not isinstance(levels, list) or len(levels) > 500:
        raise ValueError("asks requires a list of at most 500 actual price/base-quantity levels.")
    if type(snapshot.get("asks_complete")) is not bool:
        raise ValueError("asks_complete must explicitly describe captured book completeness.")
    parsed = []
    for level in levels:
        if not isinstance(level, dict) or set(level) != {"price", "quantity"}:
            raise ValueError("Ask levels require only price and quantity in base-asset units.")
        price = _decimal(level["price"], "ask price", positive=True)
        quantity = _decimal(level["quantity"], "ask quantity", positive=True)
        if parsed and price <= parsed[-1][0]:
            raise ValueError("Ask levels must have strictly increasing unique prices.")
        parsed.append((price, quantity))
    return parsed


def _eligibility(snapshot: dict) -> dict:
    evidence = snapshot.get("purchase_eligibility")
    if evidence is None:
        return {"status": "unknown", "reasons": ["entity_and_service_eligibility_not_supplied"]}
    if not isinstance(evidence, dict):
        raise ValueError("purchase_eligibility must be an object or null.")
    for field in ("jurisdiction", "legal_entity", "service", "input_reference"):
        _label(evidence.get(field), f"purchase_eligibility {field}")
    if evidence.get("status") not in ("eligible", "ineligible", "unknown"):
        raise ValueError("purchase_eligibility status must be eligible, ineligible or unknown.")
    return deepcopy(evidence)


def _fee(amount: Decimal, assumptions: dict) -> Decimal:
    fee = amount * assumptions["fee_rate"]
    step = assumptions["fee_step"]
    return (fee / step).to_integral_value(rounding=ROUND_CEILING) * step if step else fee


def _purchase(levels: list[tuple[Decimal, Decimal]], assumptions: dict, budget: Decimal) -> dict:
    quantities, costs = [], []
    for price, quantity in levels:
        quantities.append((quantities[-1] if quantities else _ZERO) + quantity)
        costs.append((costs[-1] if costs else _ZERO) + price * quantity)
    total_quantity, total_notional = quantities[-1], costs[-1]
    step = assumptions["quantity_step"]
    fixed = sum((assumptions[key] for key in ("fixed_fee_quote", "payment_cost_quote", "withdrawal_cost_quote")), _ZERO)

    def notional(quantity):
        if not quantity:
            return _ZERO
        index = bisect_left(quantities, quantity)
        previous_q = quantities[index - 1] if index else _ZERO
        previous_cost = costs[index - 1] if index else _ZERO
        return previous_cost + (quantity - previous_q) * levels[index][0]

    def cash(quantity):
        value = notional(quantity)
        return value + fixed + (_fee(value, assumptions) if assumptions["fee_currency"] == "quote" else _ZERO)

    low, high = 0, int(total_quantity // step)
    while low < high:
        middle = (low + high + 1) // 2
        if cash(step * middle) <= budget:
            low = middle
        else:
            high = middle - 1
    quantity = step * low
    value = notional(quantity)
    variable_quote = _fee(value, assumptions) if assumptions["fee_currency"] == "quote" else _ZERO
    variable_base = _fee(quantity, assumptions) if assumptions["fee_currency"] == "base" else _ZERO
    received = quantity - variable_base - assumptions["withdrawal_cost_base"]
    # If another lot could fit at even the last known ask, the
    # captured tail is insufficient. We never invent a price beyond that tail.
    next_at_tail = value + step * levels[-1][0]
    tail_cash = next_at_tail + fixed
    if assumptions["fee_currency"] == "quote":
        tail_cash += _fee(next_at_tail, assumptions)
    depth_sufficient = quantity + step <= total_quantity or tail_cash > budget
    reasons = []
    if not depth_sufficient:
        reasons.append("insufficient_amount_relevant_depth")
    if not quantity:
        reasons.append("budget_insufficient_for_costs" if budget <= fixed else "budget_below_quantity_step")
    elif quantity < assumptions["min_quantity"] or value < assumptions["min_notional"]:
        reasons.append("below_minimum_order")
    elif received <= 0:
        reasons.append("nonpositive_received_quantity")
    no_fill = bool(set(reasons) - {"insufficient_amount_relevant_depth"})
    if no_fill:
        quantity = value = variable_quote = variable_base = received = _ZERO
    spending = value + variable_quote + fixed if quantity else _ZERO
    fills, remaining = [], quantity
    for price, available in levels:
        taken = min(available, remaining)
        if taken:
            fills.append({"price": _text(price), "quantity": _text(taken), "notional_quote": _text(price * taken)})
        remaining -= taken
    return {
        "gross_quantity": _text(quantity), "quantity_received": _text(received),
        "notional_quote": _text(value), "trading_fee_quote": _text(variable_quote + (assumptions["fixed_fee_quote"] if quantity else _ZERO)),
        "trading_fee_base": _text(variable_base),
        **{key: _text(assumptions[key] if quantity else _ZERO)
           for key in ("payment_cost_quote", "withdrawal_cost_quote", "withdrawal_cost_base")},
        "all_in_cost_quote": _text(spending), "leftover_quote": _text(budget - spending),
        "all_in_unit_cost_quote": _ratio(spending / received) if received else None,
        "volume_weighted_ask": _ratio(value / quantity) if quantity else None,
        "size_relevant_depth": {
            "budget_supported_by_captured_levels": depth_sufficient,
            "captured_ask_quantity": _text(total_quantity), "captured_ask_notional_quote": _text(total_notional),
            "levels_consumed": len(fills), "fill_guaranteed": False,
        },
        "fills": fills, "reasons": reasons,
    }


def analyze_purchase_costs(request: dict, as_of: int) -> dict:
    """Estimate fee-inclusive costs for one budget using caller-supplied spot asks.

    Common selectors are asset, native quote, data_mode, sources, snapshots and
    explicit maximum age/skew seconds. Each snapshot identifies event/capture
    times and input_reference. Ask quantities are base units, sorted by price.
    Fee/rule assumptions are copied into the result. Unknown required assumptions
    suppress costs. fee_step=0 assumes exact percentage fees; positive fee_step
    rounds the percentage fee up in fee_currency. Fixed, payment and withdrawal
    quote costs consume the budget. Base fees/withdrawals reduce received units.
    Minimum quantity/notional constrain the gross order, and quantity_step rounds
    that order down. Price_step validates quoted levels rather than inventing new
    executable prices. No fees are charged for a hypothetical zero-size order.
    """
    with localcontext(Context(prec=96)):
        sources, snapshots = _common(request, as_of)
        budget = _decimal(request.get("budget"), "budget", positive=True)
        rows, level_count = [], 0
        for source in sources:
            snapshot = snapshots.get(source)
            row = _row(source, snapshot, request, as_of)
            row["hypothetical_fill"] = None
            if snapshot is not None:
                product = snapshot.get("product")
                if product is not None:
                    _label(product, "product")
                row["product"] = product
                row["purchase_eligibility"] = _eligibility(snapshot)
                row["assumptions"] = deepcopy(snapshot.get("assumptions"))
                levels = _asks(snapshot)
                level_count += len(levels)
                if level_count > 2000:
                    raise ValueError("A purchase panel supports at most 2000 total ask levels.")
                row["asks_complete"] = snapshot["asks_complete"]
                row["best_ask"] = _text(levels[0][0]) if levels else None
                row["captured_depth"] = {
                    "ask_levels": len(levels),
                    "ask_quantity": _text(sum((quantity for _, quantity in levels), _ZERO)),
                    "ask_notional_quote": _text(sum((price * quantity for price, quantity in levels), _ZERO)),
                }
                assumptions, reasons = _assumptions(snapshot)
                row["reasons"].extend(reasons)
                if product != "spot_order_book":
                    row["reasons"].append("unsupported_or_ambiguous_product")
                if not levels:
                    row["reasons"].append("missing_ask_levels")
                if assumptions and any(price % assumptions["price_step"] for price, _ in levels):
                    row["reasons"].append("ask_price_off_declared_step")
                if not row["reasons"]:
                    fill = _purchase(levels, assumptions, budget)
                    row["hypothetical_fill"] = fill
                    row["reasons"].extend(fill["reasons"])
                row["status"] = "hypothetical" if not row["reasons"] else "unsupported"
            rows.append(row)
        result = _base_result(request, as_of, "purchase_costs", rows)
        result["budget"] = _text(budget)
        result["status"] = "hypothetical" if any(row["hypothetical_fill"] for row in rows) else "unsupported"
        result["comparability"] = {
            "all_requested_sources_supported": all(row["status"] == "hypothetical" for row in rows),
            "aligned": result["timing"]["aligned"],
            "purchase_eligibility_is_separate": True,
            "ranking": None,
        }
        result["limitations"].extend([
            "Actual ask levels are required; aggregate stored depth cannot reconstruct whole-order costs.",
            "Partial captured books and hypothetical costs never guarantee a fill or measured execution quality.",
            "Costs depend on the declared product, fee tier, payment and optional withdrawal assumptions.",
            "Entity/service eligibility is caller evidence and does not establish execution quality.",
        ])
        return result


def _price(snapshot: dict, basis: str) -> tuple[Decimal, dict]:
    price = _decimal(snapshot.get("price"), "price", positive=True)
    bid = _decimal(snapshot["best_bid"], "best_bid", positive=True) if snapshot.get("best_bid") is not None else None
    ask = _decimal(snapshot["best_ask"], "best_ask", positive=True) if snapshot.get("best_ask") is not None else None
    if bid is not None and ask is not None and bid > ask:
        raise ValueError("best_bid cannot exceed best_ask.")
    if basis == "mid" and bid is not None and ask is not None and price != (bid + ask) / 2:
        raise ValueError("A mid price must equal the supplied bid/ask midpoint.")
    if basis == "best_ask" and ask is not None and price != ask:
        raise ValueError("A best_ask price must match the supplied best_ask.")
    return price, {
        "best_bid": _text(bid) if bid is not None else None,
        "best_ask": _text(ask) if ask is not None else None,
        "spread_quote": _text(ask - bid) if bid is not None and ask is not None else None,
        "spread_bps": _ratio((ask - bid) / ((ask + bid) / 2) * 10000)
        if bid is not None and ask is not None else None,
    }


def analyze_venue_disagreement(request: dict, as_of: int) -> dict:
    """Diagnose same-native-quote prices with explicit age, skew and outliers.

    price_basis is explicitly mid, last or best_ask for every input record.
    Range and pairwise differences use only fresh, clock-valid supplied records.
    A range requires at least two aligned sources. Outlier diagnostics additionally
    require at least three; the caller's outlier_threshold_bps is applied to each
    absolute deviation from the internal sample median. The median is a diagnostic
    reference only, never an index or fair-value estimate; no venue is removed.
    """
    with localcontext(Context(prec=96)):
        sources, snapshots = _common(request, as_of)
        basis = request.get("price_basis")
        if basis not in ("mid", "last", "best_ask"):
            raise ValueError("price_basis must explicitly be mid, last or best_ask.")
        threshold = _decimal(request.get("outlier_threshold_bps"), "outlier_threshold_bps", positive=True)
        if threshold > 1000000:
            raise ValueError("outlier_threshold_bps must not exceed 1000000.")
        rows, prices, usable = [], {}, []
        for source in sources:
            snapshot = snapshots.get(source)
            row = _row(source, snapshot, request, as_of)
            row.update({"price": None, "outlier": None, "median_deviation_bps": None})
            if snapshot is not None:
                price, spread = _price(snapshot, basis)
                prices[source] = price
                row.update({"price": _text(price), **spread})
                if not row["reasons"]:
                    usable.append(row)
            rows.append(row)
        result = _base_result(request, as_of, "venue_disagreement", rows)
        alignment = _alignment(usable, request)
        ready = len(usable) >= 2 and alignment["aligned"]
        result.update({
            "price_basis": basis, "status": "diagnostic" if ready else "unsupported",
            "usable_timing": alignment,
            "range": None, "pairwise": [],
            "outlier_method": {
                "name": "absolute_deviation_from_sample_median", "threshold_bps": _text(threshold),
                "supported": ready and len(usable) >= 3,
                "interpretation": "Data-quality diagnostic only; all supplied venues remain visible.",
            },
        })
        result["coverage"]["usable_sources"] = [row["source"] for row in usable]
        result["coverage"]["excluded_sources"] = [row["source"] for row in rows if row["status"] != "supported"]
        result["reasons"] = []
        if len(usable) < 2:
            result["reasons"].append("fewer_than_two_fresh_sources")
        elif not ready:
            result["reasons"].append("inter_source_clock_skew")
        if ready:
            values = sorted(prices[row["source"]] for row in usable)
            low, high = values[0], values[-1]
            midpoint = (low + high) / 2
            result["range"] = {
                "minimum_price": _text(low), "maximum_price": _text(high), "range_quote": _text(high - low),
                "range_bps": _ratio((high - low) / midpoint * 10000),
                "basis": "range divided by midpoint of extremes; descriptive, not an index",
                "sources": [row["source"] for row in usable],
            }
            for index, left in enumerate(usable):
                for right in usable[index + 1:]:
                    a, b = prices[left["source"]], prices[right["source"]]
                    result["pairwise"].append({
                        "left_source": left["source"], "right_source": right["source"],
                        "right_minus_left_quote": _text(b - a),
                        "right_minus_left_bps": _ratio((b - a) / ((a + b) / 2) * 10000),
                    })
            if len(values) >= 3:
                middle = len(values) // 2
                median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
                for row in usable:
                    deviation = abs(prices[row["source"]] - median) / median * 10000
                    row.update({"median_deviation_bps": _ratio(deviation), "outlier": deviation > threshold})
        result["limitations"].append(
            "Range and median deviations describe this supplied sample; neither is fair value or a quorum signal."
        )
        return result
