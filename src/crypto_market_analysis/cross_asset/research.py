"""Price research and explicitly hypothetical cash-funded spot accounting.

No quote conversion, forecasting, execution or portfolio reconstruction occurs
here. Cash plans use closed raw bars; adjusted prices and futures require their
own models. The caller resolves the registry instrument before invoking a plan.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from .prices import (
    _actions_as_of_validated,
    _bars_as_of_validated,
    currency,
    effective_known_at,
    number,
    object_fields,
    receipt_clocks,
    text_field,
    timestamp,
    validate_series,
)


def _annualization(value: Any) -> dict | None:
    if value is None:
        return None
    value = object_fields(value, {"periods_per_year", "convention"}, set(), "annualization")
    return {
        "periods_per_year": number(value["periods_per_year"], "annualization.periods_per_year", positive=True),
        "convention": text_field(value["convention"], "annualization.convention"),
    }


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} overflows the supported numerical range")
    return value


def _evidence(descriptor: dict, provenance: dict | None = None) -> tuple[str, list[str]]:
    limitations = [
        "Price-only descriptive research; no predictive or executable performance claim.",
        "Calendar completeness and provider qualification require their separate acceptance checks.",
    ]
    if descriptor["revision_semantics"] == "captured_history" or (provenance or {}).get("receipt_evidence") == "self_declared_history":
        evidence = "captured_history_not_historical_point_in_time"
        limitations.append("Receipt filtering of captured publisher history does not establish historical availability.")
    else:
        evidence = "declared_point_in_time_receipt_vintages"
        limitations.append("Point-in-time semantics are declared; import timestamps alone are not independently witnessed receipts.")
    if descriptor["price_basis"] == "raw":
        limitations.append("Raw-price changes may include split discontinuities and exclude cash distributions.")
    elif descriptor["price_basis"] in {"split_adjusted", "total_return"}:
        limitations.append("Publisher adjustments retain their receipt vintage; a later adjustment cannot establish an earlier known history.")
    if descriptor["price_basis"] == "total_return":
        limitations.append("Publisher total-return transformation is unverified; no cash entitlement or reinvestment is inferred.")
    if descriptor["price_basis"] == "continuous":
        limitations.append("A continuous path is derived and cannot supply executable contract fills.")
    return evidence, limitations


def analyze_series(
    series: Any, *, as_of: int, start: int | None = None, end: int | None = None, annualization: Any = None,
) -> dict:
    """Describe native intervals with method-specific positive-price gates.

    Bounds are integer UTC instants, never implicitly calendar days or sessions.
    The optional annualization convention describes the intervals supplied by the
    caller; neither 252 nor 365 is a default. Gaps are not filled or treated as
    zero returns, and volatility is explicitly per observed interval transition.
    """
    series = validate_series(series)
    annualization = _annualization(annualization)
    bars = _bars_as_of_validated(series, as_of, start=start, end=end)
    descriptor = series["descriptor"]
    evidence, limitations = _evidence(descriptor, series.get("storage_provenance"))
    closes = [bar["close"] for bar in bars]
    positive = bool(closes) and all(price > 0 for price in closes)
    enough = len(closes) >= 2
    result = {
        "schema": "market-analysis.v1", "series_id": descriptor["id"], "as_of": as_of,
        "data_mode": descriptor["data_mode"], "native_currency": descriptor["native_currency"],
        "unit": descriptor["unit"], "price_basis": descriptor["price_basis"],
        "interpretation": "price_only", "evidence": evidence,
        "calendar_id": descriptor["calendar_id"], "calendar_version": descriptor["calendar_version"],
        "window": {"start": start, "end": end, "meaning": "native_intervals_entirely_within_utc_bounds"},
        "status": "ok" if enough else "insufficient_data", "bar_count": len(bars),
        "first_end": bars[0]["end"] if bars else None, "last_end": bars[-1]["end"] if bars else None,
        "first_price": closes[0] if closes else None, "last_price": closes[-1] if closes else None,
        "absolute_change": _finite(closes[-1] - closes[0], "absolute change") if enough else None,
        "simple_return": None, "log_return": None, "maximum_drawdown": None,
        "return_volatility": None, "annualized_volatility": None,
        "annualization": annualization, "transition_count": max(0, len(closes) - 1),
        "capabilities": {
            "absolute_changes": enough, "simple_returns": enough and positive,
            "log_returns": enough and positive, "cash_plan": False, "executable_fills": False,
        },
        "unsupported": [], "limitations": limitations,
    }
    if len(bars) > 1 and any(a["end"] != b["start"] for a, b in zip(bars, bars[1:], strict=False)):
        limitations.append("Native intervals have gaps; returns span observed endpoints, with no implicit expected-calendar coverage claim.")
    if not positive:
        result["unsupported"].append("percentage_log_returns_and_relative_drawdown_require_strictly_positive_closes")
    if annualization is None:
        result["unsupported"].append("annualized_volatility_requires_explicit_annualization")
    if enough and positive:
        simple = [_finite(right / left - 1, "simple return") for left, right in zip(closes, closes[1:], strict=False)]
        result["simple_return"] = _finite(closes[-1] / closes[0] - 1, "simple return")
        # Differences of logs avoid ratio underflow and overflow.
        result["log_return"] = math.log(closes[-1]) - math.log(closes[0])
        peak = closes[0]
        drawdown = 0.0
        for close in closes:
            peak = max(peak, close)
            drawdown = min(drawdown, close / peak - 1)
        result["maximum_drawdown"] = drawdown
        if len(simple) >= 2:
            volatility = _finite(statistics.stdev(simple), "return volatility")
            result["return_volatility"] = volatility
            if annualization is not None:
                result["annualized_volatility"] = _finite(
                    volatility * math.sqrt(annualization["periods_per_year"]), "annualized volatility",
                )
    return result


def _plan_request(request: Any) -> dict:
    request = object_fields(
        request,
        {"currency", "initial_cash", "deposits", "purchases", "fee_per_trade", "distributions", "actions_complete"},
        set(), "cash plan",
    )
    currency(request["currency"], "cash plan.currency")
    number(request["initial_cash"], "cash plan.initial_cash", minimum=0)
    if request["fee_per_trade"] is not None:
        number(request["fee_per_trade"], "cash plan.fee_per_trade", minimum=0)
    if request["distributions"] != "cash":
        raise ValueError("cash plan supports only explicit cash distributions, without reinvestment")
    if type(request["actions_complete"]) is not bool:
        raise ValueError("cash plan.actions_complete must be a boolean declaration")
    for field, amount_key in (("deposits", "amount"), ("purchases", "cash_amount")):
        if not isinstance(request[field], list):
            raise ValueError(f"cash plan.{field} must be an array")
        if len(request[field]) > 2_000:
            raise ValueError(f"cash plan.{field} exceeds the bounded 2000 entry limit")
        seen: set[int] = set()
        for row in request[field]:
            object_fields(row, {"at", amount_key}, set(), f"cash plan.{field} entry")
            timestamp(row["at"], f"cash plan.{field}.at")
            number(row[amount_key], f"cash plan.{field}.{amount_key}", minimum=0)
            if row["at"] in seen:
                raise ValueError(f"cash plan.{field} timestamps must be unique")
            seen.add(row["at"])
    return request


def _cash_instrument(instrument: Any, descriptor: dict) -> None:
    if not isinstance(instrument, dict):
        raise ValueError("cash plan requires a resolved registry instrument")
    if instrument.get("id") != descriptor["instrument_id"]:
        raise ValueError("cash plan instrument identity does not match series")
    pair = (instrument.get("asset_class"), instrument.get("form"))
    if any(not isinstance(value, str) for value in pair) or pair not in {
        ("equity", "share"), ("equity", "fund"), ("commodity", "fund"), ("crypto", "spot"),
    }:
        raise ValueError("cash plan requires an accepted share, listed fund or spot cryptocurrency; futures and references are unsupported")
    if descriptor["price_basis"] != "raw" or descriptor["interval"] == "reference":
        raise ValueError("cash plan requires raw traded prices, not adjusted, reference or continuous paths")
    if descriptor["listing_id"] is None:
        raise ValueError("cash plan requires a resolved listing")


def _action_completeness(series: dict, request: dict, start: int, as_of: int) -> bool:
    coverage = series["descriptor"].get("action_coverage")
    if not request["actions_complete"] or coverage is None:
        return False
    return (
        coverage["status"] == "declared_complete" and coverage["known_at"] <= as_of
        and coverage["start"] <= start and coverage["end"] >= as_of
        and set(coverage["kinds"]) == {"split", "cash_dividend"}
    )


def simulate_cash_plan(series: Any, request: Any, *, as_of: int, instrument: dict) -> dict:
    """Hypothetical raw-price cash purchases, splits and non-reinvested dividends.

    A purchase occurs exactly at a closed bar's end using the latest vintage
    received by that same instant. A later correction cannot change the fill.
    Deposits precede actions, which precede purchases at a shared timestamp.
    Dividend entitlement uses units held immediately before its effective clock;
    payment credits cash only at pay_at. A late-known action affecting existing
    holdings fails explicitly, since reconstructing it would invent information.
    """
    series = validate_series(series)
    request = _plan_request(request)
    timestamp(as_of, "as_of")
    descriptor = series["descriptor"]
    _cash_instrument(instrument, descriptor)
    if request["currency"] != descriptor["native_currency"]:
        raise ValueError("cash plan currency must match native currency; no implicit FX conversion")
    bars = _bars_as_of_validated(series, as_of)
    if not bars:
        raise ValueError("cash plan has no closed price known at as_of")
    first = min([bars[0]["start"], *(p["at"] for p in request["purchases"]), *(d["at"] for d in request["deposits"])])
    complete = _action_completeness(series, request, first, as_of)
    evidence, limitations = _evidence(descriptor, series.get("storage_provenance"))
    limitations.append("Hypothetical close-price fills omit spreads, slippage, taxes and execution access; Ledgerly remains actual-holdings authority.")
    limitations.append("Action coverage is a source declaration, not independent verification; undisclosed or not-yet-received actions remain unknown.")
    if not complete:
        limitations.append("Action coverage is unknown or incomplete; wealth includes only known actions and is not a complete total-return result.")
    fees_known = request["fee_per_trade"] is not None
    fee = float(request["fee_per_trade"] or 0)
    if not fees_known:
        limitations.append("Trading fees are unknown and excluded from the numerical result.")
    cash = float(request["initial_cash"])
    contributed = cash
    units = dividends = fees = 0.0
    receivables: dict[str, float] = {}
    fills: list[dict] = []
    events: list[tuple[int, int, str, dict]] = []
    action_kinds: dict[int, set[str]] = {}
    applied_action_times: list[int] = []
    split_effective_times: set[int] = set()
    bar_clocks = receipt_clocks(series, "bars")
    action_clocks = receipt_clocks(series, "actions")
    fill_bars: dict[int, dict] = {}
    fill_ranks: dict[int, tuple[int, int]] = {}
    # Each exact close has its own purchase cutoff. Index once: historical fills
    # cannot use corrections received after that close, even at a later valuation.
    for candidate in series["bars"]:
        known_at = effective_known_at(series, candidate, "bars", receipt_index=bar_clocks)
        rank = (known_at, candidate["observed_at"])
        at = candidate["end"]
        if known_at <= at and (at not in fill_ranks or rank > fill_ranks[at]):
            fill_ranks[at] = rank
            fill_bars[at] = candidate
    for deposit in request["deposits"]:
        if deposit["at"] <= as_of:
            events.append((deposit["at"], 0, "deposit", deposit))
    for purchase in request["purchases"]:
        if purchase["at"] <= as_of:
            events.append((purchase["at"], 3, "purchase", purchase))
    for action in _actions_as_of_validated(series, as_of):
        if action["effective_at"] <= as_of:
            events.append((action["effective_at"], 1, "action", action))
            action_kinds.setdefault(action["effective_at"], set()).add(action["kind"])
            if action["kind"] == "split":
                split_effective_times.add(action["effective_at"])
            if action["kind"] == "cash_dividend" and "pay_at" in action and action["pay_at"] <= as_of:
                events.append((action["pay_at"], 2, "payment", action))
    # Stable order by explicit identity avoids input order affecting same-time actions.
    events.sort(key=lambda row: (row[0], row[1], row[3].get("id", "")))
    for at, _, kind, event in events:
        if kind == "deposit":
            cash += event["amount"]
            contributed += event["amount"]
        elif kind == "purchase":
            if at in split_effective_times:
                raise ValueError("purchase at the exact split-effective instant requires an explicit post-split price basis")
            eligible = fill_bars.get(at)
            if eligible is None:
                raise ValueError("purchase must match a closed bar end with a vintage already received at that instant")
            price = eligible["close"]
            if price <= 0:
                raise ValueError("cash plan requires strictly positive purchase prices")
            amount = event["cash_amount"]
            if amount == 0:
                continue
            cost = _finite(amount + fee, "purchase cost")
            if cost > cash:
                raise ValueError("cash plan purchase and explicit fee exceed available cash")
            acquired = _finite(amount / price, "purchased units")
            units += acquired
            cash -= cost
            fees += fee
            fills.append({"at": at, "price": price, "units": acquired, "cash_amount": amount, "fee": fee if fees_known else None, "bar_observed_at": eligible["observed_at"]})
        elif kind == "action":
            if units == 0:
                continue
            if action_kinds[at] == {"split", "cash_dividend"}:
                raise ValueError("simultaneous split and cash dividend require an explicit dividend amount basis; unsupported ambiguous entitlement")
            if effective_known_at(series, event, "actions", receipt_index=action_clocks) > at:
                raise ValueError("late-known corporate action affects held units; historical execution cannot be reconstructed point in time")
            applied_action_times.append(at)
            if event["kind"] == "split":
                units *= event["ratio"]
            else:
                if event.get("currency") != descriptor["native_currency"]:
                    raise ValueError("dividend currency is missing or differs from the plan; no implicit FX conversion")
                if "pay_at" not in event:
                    raise ValueError("dividend payment clock is required for cash wealth")
                receivables[event["id"]] = _finite(units * event["amount"], "dividend entitlement")
        elif kind == "payment":
            payment = receivables.pop(event["id"], 0.0)
            cash += payment
            dividends += payment
        for name, value in (("cash", cash), ("units", units), ("contributions", contributed), ("fees", fees), ("dividends", dividends)):
            _finite(value, name)
    mark = bars[-1]
    if applied_action_times and mark["end"] <= max(applied_action_times):
        raise ValueError("cash wealth requires a post-action price mark; a stale pre-action close cannot value transformed holdings or dividend entitlement")
    if mark["close"] <= 0:
        raise ValueError("cash plan requires a strictly positive valuation price")
    position = _finite(units * mark["close"], "marked position")
    pending = _finite(sum(receivables.values()), "dividend receivables")
    return {
        "schema": "market-cash-plan.v1", "series_id": descriptor["id"], "instrument_id": instrument["id"],
        "as_of": as_of, "data_mode": descriptor["data_mode"], "currency": descriptor["native_currency"],
        "model": "hypothetical_cash_funded_spot", "evidence": evidence,
        "interpretation": "known_actions_cash_distributions" if complete else "price_only_with_known_actions_incomplete",
        "action_coverage": "source_declared_complete" if complete else "unknown_or_incomplete",
        "fees": {"status": "explicit" if fees_known else "unknown_excluded", "paid": fees if fees_known else None},
        "units": units, "cash": cash, "position_value": position, "cash_wealth": _finite(cash + position, "cash wealth"),
        "dividend_receivables": pending, "wealth_including_receivables": _finite(cash + position + pending, "accrued wealth"),
        "dividends_paid": dividends, "contributed": contributed,
        "mark": {"price": mark["close"], "end": mark["end"], "observed_at": mark["observed_at"]},
        "fills": fills, "distributions": "cash_no_reinvestment", "limitations": limitations,
    }
