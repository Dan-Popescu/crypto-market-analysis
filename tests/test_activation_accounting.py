"""Hand-calculated cash invariants through the captured brief's public views."""

from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_market_analysis.accumulation_brief import build_accumulation_brief
from crypto_market_analysis.evidence import CapturedInputs
from crypto_market_analysis.models import Candle
from crypto_market_analysis.plan_builder import construct_plan

START = 3600
DAY = 86400


def config(**changes):
    return construct_plan({"asset": "BTC", "quote": "EUR", "source": "reference", "data_mode": "demo",
        "start_date": "1970-01-01T01:00:00Z", "recurrence": {"frequency": "daily", "count": 1},
        "contribution_amount": "100", "purchase_amount": "100", **changes})


def policy_at(plan, as_of):
    candle = Candle("reference", "BTC", "EUR", "1h", 0, START, 100, 100, 100, 100, 1, START,
                    data_mode="demo")
    bars = tuple(replace(candle, open_time=t, close_time=t + START, observed_at=t + START)
                 for t in range(0, as_of, START))
    captured = CapturedInputs(as_of, "BTC", "demo", "reference", "EUR", bars,
                             (), (), (), {}, {"candles": len(bars)}, {"market_interval": START}, {})
    return build_accumulation_brief(captured, plan)["hypothetical_policy"]


@pytest.mark.parametrize("family,changes,deposit,orders,required,allocated,locked,overflow", [
    # Contributions are available before allocation; positive split slices each incur a fixed fee.
    ("split", {"fees": {"rate": "0.01", "fixed": "1"}}, "100", ["100"],
     ["51.5", "51.5"], ["51.5", "48.5"], ["0", "48.5"], ["0", "0"]),
    # Base fees do not require quote cash; the target cap redirects existing cash only.
    ("deferred", {"fees": {"asset": "base", "fixed": "0.001"},
                  "plans": {"deferred": {"fraction": "1", "max_deferred_reserve_quote": "60"}}},
     "100", ["100"], ["100"], ["100"], ["60"], ["40"]),
    # Same-time orders remain separate and cannot spend another order's assigned cash.
    ("regular", {"fees": {"fixed": "1"}}, "101", ["50", "50"],
     ["51", "51"], ["51", "50"], ["0", "0"], ["0", "0"]),
    ("split", {"fees": {"fixed": "1"}}, "101", ["50", "50"],
     ["26", "26", "26", "26"], ["26", "26", "26", "23"],
     ["0", "26", "0", "23"], ["0", "0", "0", "0"]),
    # The cap is shared across simultaneous activations, including earlier slices.
    ("deferred", {"plans": {"deferred": {"fraction": "1", "max_deferred_reserve_quote": "60"}}},
     "200", ["100", "100"], ["100", "100"], ["100", "100"], ["60", "0"], ["40", "100"]),
    # Zero caps and zero-sized initial slices do not introduce an extra fixed fee.
    ("split", {"fees": {"fixed": "1"}, "plans": {"split": {"initial_fraction": "0",
        "levels": [{"decline": "0.1", "fraction": "1"}], "max_deferred_reserve_quote": "0"}}},
     "100", ["100"], ["101"], ["100"], ["0"], ["100"]),
])
def test_activation_cash_invariant_matrix(family, changes, deposit, orders, required, allocated, locked, overflow):
    plan = config(**changes)
    plan["contributions"] = [{"time": START, "amount": deposit}]
    plan["schedule"] = [{"time": START, "amount": amount} for amount in orders]
    policy = policy_at(plan, START)
    brief = policy["next_tranche"]["families"][family]
    preview = policy["funding_preview"]["families"][family]
    simulation = policy["current_simulation"]["plans"][family]
    for view in (brief, {"allocations": [allocation for row in preview["tranches"]
                                        for allocation in row["allocations"]]}):
        assert [row["required_cash"] for row in view["allocations"]] == required
        assert [row["allocated_cash"] for row in view["allocations"]] == allocated
        assert [row["locked_cash"] for row in view["allocations"]] == locked
        assert [row["cap_overflow_immediate_cash"] for row in view["allocations"]] == overflow
    assert [row["allocated_cash"] for row in simulation["funding"]] == allocated
    assert brief["current_cash_available"] == deposit
    assert brief["free_cash_after"] == simulation["unallocated_reserve"] == preview["current"]["free_cash"]
    assert Decimal(brief["locked_cash_after"]) == sum(map(Decimal, allocated))
    assert Decimal(brief["free_cash_after"]) + Decimal(brief["locked_cash_after"]) == Decimal(deposit)
    assert simulation["history"] == []  # No completed post-activation bar exists yet.


@pytest.mark.parametrize("fallback", ["reserve", "buy_at_deadline"])
@pytest.mark.parametrize("cap", [None, "60"])
@pytest.mark.parametrize("deposit", ["0", "25"])
def test_due_deposits_and_expiry_precede_commitments_without_reusing_waiting_cash(fallback, cap, deposit):
    plan = config(plans={"deferred": {"fraction": "1", "decline": "0.5", "max_wait_days": 7,
                                      "fallback": fallback, "max_deferred_reserve_quote": cap}})
    deadline = START + 7 * DAY
    plan["schedule"].append({"time": deadline, "amount": "100"})
    if deposit != "0":
        plan["contributions"].append({"time": deadline, "amount": deposit})
    policy = policy_at(plan, deadline)
    brief = policy["next_tranche"]["families"]["deferred"]
    simulated = policy["current_simulation"]["plans"]["deferred"]
    preview = policy["funding_preview"]["families"]["deferred"]["next_tranche"]["allocations"][0]
    previous_target = Decimal(cap or "100")
    available = Decimal(deposit) + (previous_target if fallback == "reserve" else 0)
    allocated = min(Decimal(100), available)
    for row in (brief["allocations"][0], simulated["funding"][-1], preview):
        assert Decimal(row["allocated_cash"]) == allocated
        assert row["funding_shortfall"] == (allocated < 100)
    assert Decimal(brief["current_cash_available"]) == available
    assert brief["target_locked_cash_before"] == "0"  # Buy fallback also stops consuming the target cap.
    waiting_before = previous_target if fallback == "buy_at_deadline" else 0
    assert Decimal(brief["locked_cash_after"]) == waiting_before + allocated
    assert Decimal(simulated["reserve"]) + Decimal(simulated["amount_spent"]) == 100 + Decimal(deposit)


def test_future_capacity_keeps_waiting_cash_exclusive_and_releases_unknown_until_due():
    plan = config(plans={"deferred": {"fraction": "1", "max_wait_days": 7, "fallback": "reserve"}})
    deadline = START + 7 * DAY
    plan["schedule"].append({"time": deadline, "amount": "100"})
    plan["contributions"].append({"time": deadline, "amount": "25"})
    policy = policy_at(plan, 2 * START)
    next_tranche = policy["next_tranche"]
    deferred = next_tranche["families"]["deferred"]
    target = deferred["allocations"][0]
    assert next_tranche["future_anchor_unknown"]
    assert deferred["current_cash_available"] == "0"
    assert deferred["target_locked_cash_before"] == "100"
    assert deferred["future_contributions_before_activation"] == "25"
    assert target["allocated_cash"] == target["funded_from_future_contributions"] == "25"
    assert target["funded_from_current_cash"] == "0"
    assert target["funding_shortfall"]
    assert target["activation_anchor_status"] == "unknown_until_future_activation"
    assert target["target_interpretation"] == "indicative_current_anchor_not_future_execution_anchor"
    assert deferred["locked_cash_after"] == "125"
    # The separate price-free schedule projects deterministic reserve fallback;
    # the next-tranche illustration makes no future market-path claim.
    preview = policy["funding_preview"]["families"]["deferred"]["next_tranche"]["allocations"][0]
    assert preview["allocated_cash"] == "100"
