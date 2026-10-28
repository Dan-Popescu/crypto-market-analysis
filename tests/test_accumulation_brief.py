"""Single captured-market review separates observed, future and historical data."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_market_analysis.accumulation_brief import build_accumulation_brief
from crypto_market_analysis.evidence import CapturedInputs
from crypto_market_analysis.models import Candle
from crypto_market_analysis.plan_builder import construct_plan


def capture(as_of=3600, candles=None):
    candle = Candle("reference", "BTC", "EUR", "1h", 0, 3600, 100, 100, 100, 100, 1, 3600, data_mode="demo")
    return CapturedInputs(as_of, "BTC", "demo", "reference", "EUR", tuple([candle] if candles is None else candles),
                          (), (), (), {}, {"candles": 1}, {"market_interval": 3600}, {})


def plan(start="1970-01-01T01:00:00Z", **changes):
    return construct_plan({"asset": "BTC", "quote": "EUR", "source": "reference", "data_mode": "demo",
        "start_date": start, "recurrence": {"frequency": "daily", "count": 1},
        "contribution_amount": "100", "purchase_amount": "100", **changes})


def test_one_capture_identity_reference_and_no_personal_plan_needed():
    captured = capture()
    before = captured.evidence()
    result = build_accumulation_brief(captured)
    assert result["as_of"] == 3600
    assert result["market"] == {"source": "reference", "asset": "BTC", "quote": "EUR", "data_mode": "demo"}
    observed = result["current_observed"]
    assert observed["anchor"]["price"] == "100"
    assert observed["anchor"]["age_seconds"] == 0
    for section in (observed, observed["context"], observed["purchase_windows"], result["hypothetical_policy"]):
        assert section["input_reference"] == result["input_reference"]
    assert observed["purchase_windows"]["market"] == result["market"]
    assert set(observed["purchase_windows"]["windows"]) == {"1", "3", "7", "14"}
    assert observed["context"]["lookbacks"]["730"]["return"]["status"] == "unavailable"
    assert result["hypothetical_policy"]["status"] == "not_supplied"
    assert captured.evidence() == before


def test_exact_activation_preview_does_not_count_current_allocation_twice():
    config = plan(fees={"asset": "quote", "fixed": "1", "rate": "0.01"})
    original = deepcopy(config)
    result = build_accumulation_brief(capture(), config)
    next_tranche = result["hypothetical_policy"]["next_tranche"]
    regular = next_tranche["families"]["regular"]
    assert regular["current_cash_available"] == "100"
    assert regular["allocations"][0]["required_cash"] == "102"
    assert regular["allocations"][0]["allocated_cash"] == "100"
    assert regular["funding_shortfall"]
    split = next_tranche["families"]["split"]
    assert split["immediate_notional"] == "50"
    assert split["deferred_notional"] == "50"
    assert split["allocations"][1]["target_price"] == "95"
    assert split["allocations"][1]["deadline"] == 3600 + 7 * 86400
    assert split["allocations"][1]["fallback"] == "buy_at_deadline"
    assert result["historical_outcomes"]["90"]["status"] == "pending"
    assert result["historical_outcomes"]["180"]["status"] == "pending"
    assert original == config


def test_future_tranche_deposits_are_distinct_and_targets_only_indicative():
    config = plan(start="1970-01-02T01:00:00Z")
    result = build_accumulation_brief(capture(), config)
    policy = result["hypothetical_policy"]
    assert policy["current_simulation"] is None
    next_tranche = policy["next_tranche"]
    regular = next_tranche["families"]["regular"]
    assert regular["current_cash_available"] == "0"
    assert regular["future_contributions_before_activation"] == "100"
    assert regular["allocations"][0]["funded_from_current_cash"] == "0"
    assert regular["allocations"][0]["funded_from_future_contributions"] == "100"
    assert next_tranche["future_anchor_unknown"]
    target = next_tranche["families"]["deferred"]["allocations"][0]
    assert target["target_price"] == "95"
    assert target["target_interpretation"] == "indicative_current_anchor_not_future_execution_anchor"


def test_stale_at_exact_boundary_suppresses_price_targets_and_conditional_frequencies():
    result = build_accumulation_brief(capture(10800), plan(start="1970-01-02T01:00:00Z"))
    observed = result["current_observed"]
    assert observed["anchor"]["price"] == "100"  # the dated observation remains visible
    assert observed["anchor"]["stale"]
    assert not observed["anchor"]["current_price_interpretation_available"]
    target = result["hypothetical_policy"]["next_tranche"]["families"]["deferred"]["allocations"][0]
    assert target["target_price"] is None
    assert target["target_interpretation"] == "suppressed_stale_or_missing_anchor"
    assert all(row["conditional_frequencies"] is None for rows in observed["purchase_windows"]["windows"].values() for row in rows.values())


def test_missing_or_later_available_anchor_remains_unknown():
    late = replace(capture().candles[0], observed_at=3601)
    result = build_accumulation_brief(capture(candles=[late]), plan())
    assert result["current_observed"]["anchor"]["price"] is None
    assert result["current_observed"]["anchor"]["status"] == "unavailable"
    target = result["hypothetical_policy"]["next_tranche"]["families"]["deferred"]["allocations"][0]
    assert target["target_price"] is None
    assert target["requested_notional"] == "100"
    assert target["deadline"] == 3600 + 7 * 86400


@pytest.mark.parametrize("field,value", [("source", "other"), ("quote", "USD"), ("asset", "ETH"), ("data_mode", "import")])
def test_plan_identity_cannot_cross_native_quote_source_asset_or_mode(field, value):
    config = plan()
    config[field] = value
    with pytest.raises(ValueError, match="must match"):
        build_accumulation_brief(capture(), config)


def test_pinned_identity_and_cutoff_required_and_future_evidence_not_used():
    for captured in (replace(capture(), source=None), replace(capture(), quote=None), replace(capture(), as_of=True)):
        with pytest.raises(ValueError):
            build_accumulation_brief(captured)
    with pytest.raises(ValueError, match="pinned"):
        build_accumulation_brief(capture(candles=[replace(capture().candles[0], quote="USD")]))
    future = replace(capture().candles[0], open_time=3600, close_time=7200, observed_at=7200, close=200, high=200)
    result = build_accumulation_brief(capture(candles=[capture().candles[0], future]))
    assert result["current_observed"]["anchor"]["price"] == "100"


def test_sparse_terminal_valuation_is_not_a_comparable_execution_path():
    config = plan(start="1970-01-01T00:00:00Z")
    bars = [replace(capture().candles[0], open_time=day * 86400 - 3600,
                    close_time=day * 86400, observed_at=day * 86400) for day in (90, 180)]
    result = build_accumulation_brief(capture(180 * 86400, bars), config)
    for days in ("90", "180"):
        outcome = result["historical_outcomes"][days]
        assert outcome["status"] == "insufficient"
        assert outcome["reason"] == "execution_path_incomplete"
        assert outcome["comparisons"] == {}


def test_completed_historical_outcomes_compare_holdings_plus_cash_with_regular():
    config = plan()
    # Every hour, including the pre-activation anchor, is present.
    bars = [replace(capture().candles[0], open_time=t, close_time=t + 3600, observed_at=t + 3600)
            for t in range(0, 3600 + 90 * 86400, 3600)]
    result = build_accumulation_brief(capture(3600 + 90 * 86400, bars), config)
    outcome = result["historical_outcomes"]["90"]
    assert outcome["status"] == "complete"
    assert outcome["path_coverage"]["complete"]
    assert outcome["comparisons"]["split"]["terminal_wealth_difference"] == "0"
    assert result["historical_outcomes"]["180"]["status"] == "pending"


def test_pending_target_shows_frozen_amount_deadline_and_declared_fallback():
    config = plan(plans={"deferred": {"fraction": "1", "fallback": "reserve"}})
    second = replace(capture().candles[0], open_time=3600, close_time=7200, observed_at=7200)
    result = build_accumulation_brief(capture(7200, [capture().candles[0], second]), config)
    target = result["hypothetical_policy"]["pending_targets"]["deferred"][0]
    assert target["target"] == "95"
    assert target["allocated_cash"] == "100"
    assert target["deadline"] == 3600 + 7 * 86400
    assert target["fallback"] == "reserve"
    assert target["waiting_seconds"] == 3600


def test_distinct_orders_at_same_timestamp_retain_per_order_fixed_fees():
    config = plan(fees={"fixed": "1"})
    config["schedule"] = [{"time": 3600, "amount": "50"}, {"time": 3600, "amount": "50"}]
    config["contributions"][0]["amount"] = "101"
    result = build_accumulation_brief(capture(), config)
    regular = result["hypothetical_policy"]["next_tranche"]["families"]["regular"]
    assert [row["required_cash"] for row in regular["allocations"]] == ["51", "51"]
    assert [row["allocated_cash"] for row in regular["allocations"]] == ["51", "50"]
    assert regular["funding_shortfall"]


def test_fresh_market_anchor_can_be_outside_declared_plan_tolerance():
    config = plan(start="1970-01-01T01:30:00Z", execution={"max_price_age_seconds": 60})
    result = build_accumulation_brief(capture(5400), config)
    assert result["current_observed"]["anchor"]["status"] == "fresh"
    target = result["hypothetical_policy"]["next_tranche"]["families"]["deferred"]["allocations"][0]
    assert target["target_price"] is None
    assert target["activation_anchor_status"] == "unavailable"
    assert target["target_interpretation"] == "suppressed_activation_anchor_outside_plan_tolerance"


def test_usd_is_a_separate_native_market_without_conversion():
    config = plan()
    config["quote"] = "USD"
    usd = replace(capture(), quote="USD", candles=(replace(capture().candles[0], quote="USD", open=200,
                                                        high=200, low=200, close=200),))
    result = build_accumulation_brief(usd, config)
    assert result["market"]["quote"] == "USD"
    assert result["current_observed"]["anchor"]["price"] == "200"
    target = result["hypothetical_policy"]["next_tranche"]["families"]["deferred"]["allocations"][0]
    assert target["target_price"] == "190"


def test_waiting_immediate_cash_does_not_consume_the_dip_target_cap():
    config = plan(plans={"split": {"max_deferred_reserve_quote": "100"}})
    config["schedule"].append({"time": 7200, "amount": "100"})
    config["contributions"][0]["amount"] = "200"
    result = build_accumulation_brief(capture(5400), config)
    policy = result["hypothetical_policy"]
    current = policy["current_simulation"]["plans"]["split"]
    assert current["unallocated_reserve"] == "100"
    assert current["locked_reserve"] == "100"
    assert sorted(row["target"] or "immediate" for row in current["waiting_targets"]) == ["95", "immediate"]
    split = policy["next_tranche"]["families"]["split"]
    target = next(row for row in split["allocations"] if row["decline"] is not None)
    assert target["locked_cash"] == "50"
    assert target["cap_overflow_immediate_cash"] == "0"
    assert split["target_locked_cash_before"] == "50"
    assert split["target_locked_cash_after"] == "100"
    assert split["locked_cash_after"] == "200"
    assert Decimal(split["free_cash_after"]) + Decimal(split["locked_cash_after"]) == Decimal(current["reserve"])
    assert current["reserve"] == "200"


def test_brief_reuses_execution_coverage_after_all_intents_resolve():
    config = plan(plans={"split": {"fallback": "reserve"},
                         "deferred": {"fraction": "1", "fallback": "reserve"}})
    terminal = 3600 + 90 * 86400
    bars = [replace(capture().candles[0], open_time=t, close_time=t + 3600, observed_at=t + 3600)
            for t in range(0, 8 * 86400, 3600)]
    bars.append(replace(capture().candles[0], open_time=terminal - 3600,
                        close_time=terminal, observed_at=terminal))
    result = build_accumulation_brief(capture(terminal, bars), config)
    outcome = result["historical_outcomes"]["90"]
    assert outcome["status"] == "complete"
    assert outcome["path_coverage"]["complete"]
    assert outcome["comparisons"]["deferred"]["terminal_wealth_difference"] == "0"
    for family, path in outcome["path_coverage"]["execution_paths"].items():
        terminal_coverage = result["hypothetical_policy"]["current_simulation"]["plans"][family]["terminals"]["90"]["execution_coverage"]
        assert path == terminal_coverage
        assert path["qualified"]
