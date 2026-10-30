"""Independent fee/cash hand calculations and bounded snapshot diagnostics."""

import json
from copy import deepcopy
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from pathlib import Path

import pytest

from crypto_market_analysis.venue_analysis import analyze_purchase_costs, analyze_venue_disagreement

NOW = 1000


def snapshot(source="a", **changes):
    return {
        "source": source, "asset": "BTC", "quote": "EUR", "data_mode": "demo",
        "event_time": 990, "captured_at": 991, "input_reference": "synthetic-book-v1",
        "product": "spot_order_book", "asks_complete": False,
        "asks": [{"price": "100", "quantity": "2"}],
        "assumptions": {
            "input_reference": "synthetic-fee-and-minimum-assumptions-v1", "fee_tier": "synthetic taker",
            "fee_rate": "0", "fee_currency": "quote", "fee_step": "0", "fixed_fee_quote": "0",
            "payment_cost_quote": "0", "withdrawal_cost_quote": "0", "withdrawal_cost_base": "0",
            "quantity_step": "0.01", "price_step": "0.01", "min_quantity": "0", "min_notional": "0",
        }, **changes,
    }


def request(rows=None, **changes):
    rows = [snapshot()] if rows is None else rows
    return {
        "asset": "BTC", "quote": "EUR", "data_mode": "demo", "budget": "100",
        "sources": [row["source"] for row in rows], "snapshots": rows,
        "max_age_seconds": 60, "max_skew_seconds": 5, **changes,
    }


def costs(data):
    return analyze_purchase_costs(data, NOW)["rows"][0]


def price(source="a", value="100", **changes):
    return snapshot(source, price=value, **changes)


def diagnostic(rows=None, **changes):
    rows = [price(), price("b", "110")] if rows is None else rows
    return request(rows, **{"price_basis": "last", "outlier_threshold_bps": "100", **changes})


def test_multilevel_inclusive_quote_fees_exact_cash_and_provenance():
    row = snapshot(asks=[{"price": "100", "quantity": "0.5"}, {"price": "120", "quantity": "1"}])
    row["assumptions"].update(fee_rate="0.01", fixed_fee_quote="1")
    result = costs(request([row], budget="112.1"))
    fill = result["hypothetical_fill"]
    assert result["status"] == "hypothetical"
    assert fill["gross_quantity"] == fill["quantity_received"] == "1"
    assert fill["notional_quote"] == fill["volume_weighted_ask"] == "110"
    assert fill["trading_fee_quote"] == "2.1"
    assert fill["all_in_cost_quote"] == fill["all_in_unit_cost_quote"] == "112.1"
    assert fill["leftover_quote"] == "0"
    assert [f["quantity"] for f in fill["fills"]] == ["0.5", "0.5"]
    assert fill["size_relevant_depth"]["budget_supported_by_captured_levels"] is True
    assert fill["size_relevant_depth"]["fill_guaranteed"] is False
    assert result["input_reference"] == row["input_reference"]
    assert result["assumptions"] == row["assumptions"]


def test_quantity_step_applies_to_total_order_not_separate_level_chunks():
    row = snapshot(asks=[{"price": "100", "quantity": "0.25"}, {"price": "120", "quantity": "1"}])
    row["assumptions"]["quantity_step"] = "0.1"
    fill = costs(request([row], budget="40"))["hypothetical_fill"]
    assert fill["gross_quantity"] == "0.3"
    assert fill["notional_quote"] == "31"  # 25 + 6
    assert fill["leftover_quote"] == "9"
    assert [f["quantity"] for f in fill["fills"]] == ["0.25", "0.05"]


def test_fee_quantum_rounds_up_and_cash_budget_clips_at_whole_lots():
    row = snapshot(asks=[{"price": "3", "quantity": "100"}])
    row["assumptions"].update(fee_rate="0.01", fee_step="0.1", fixed_fee_quote="0.2", quantity_step="1")
    fill = costs(request([row], budget="10"))["hypothetical_fill"]
    assert fill["gross_quantity"] == "3"
    assert fill["notional_quote"] == "9"
    assert fill["trading_fee_quote"] == "0.3"  # ceil(0.09/0.1)*0.1 + 0.2
    assert fill["all_in_cost_quote"] == "9.3"
    assert fill["leftover_quote"] == "0.7"


def test_base_fee_and_fixed_payment_withdrawal_costs_use_distinct_units():
    row = snapshot()
    row["assumptions"].update(fee_currency="base", fee_rate="0.01", fee_step="0.001",
                              fixed_fee_quote="1", payment_cost_quote="1", withdrawal_cost_quote="1",
                              withdrawal_cost_base="0.02")
    fill = costs(request([row], budget="103"))["hypothetical_fill"]
    assert fill["gross_quantity"] == "1"
    assert fill["quantity_received"] == "0.97"
    assert fill["notional_quote"] == "100"
    assert fill["trading_fee_quote"] == "1"
    assert fill["trading_fee_base"] == "0.01"
    assert fill["all_in_cost_quote"] == "103"
    assert fill["leftover_quote"] == "0"
    assert fill["all_in_unit_cost_quote"] == "106.185567010309278351"


@pytest.mark.parametrize("budget", ["1", "0.5"])
def test_budget_no_larger_than_fixed_costs_leaves_cash_uncharged(budget):
    row = snapshot()
    row["assumptions"]["fixed_fee_quote"] = "1"
    result = costs(request([row], budget=budget))
    fill = result["hypothetical_fill"]
    assert result["status"] == "unsupported"
    assert "budget_insufficient_for_costs" in result["reasons"]
    assert fill["all_in_cost_quote"] == fill["trading_fee_quote"] == fill["quantity_received"] == "0"
    assert fill["leftover_quote"] == budget


def test_quantity_dust_conserves_budget_without_buying_fractional_lot():
    row = snapshot(asks=[{"price": "3", "quantity": "100"}])
    row["assumptions"]["quantity_step"] = "0.1"
    fill = costs(request([row], budget="10"))["hypothetical_fill"]
    assert (fill["gross_quantity"], fill["all_in_cost_quote"], fill["leftover_quote"]) == ("3.3", "9.9", "0.1")


@pytest.mark.parametrize("field,value", [("min_quantity", "2"), ("min_notional", "101")])
def test_minimum_rules_suppress_orders_and_all_fixed_charges(field, value):
    row = snapshot()
    row["assumptions"].update({field: value, "payment_cost_quote": "1"})
    result = costs(request([row]))
    assert "below_minimum_order" in result["reasons"]
    assert result["hypothetical_fill"]["all_in_cost_quote"] == "0"
    assert result["hypothetical_fill"]["payment_cost_quote"] == "0"
    assert result["hypothetical_fill"]["leftover_quote"] == "100"


@pytest.mark.parametrize("complete", [False, True])
def test_insufficient_captured_depth_reports_partial_only_never_guarantees(complete):
    row = snapshot(asks=[{"price": "100", "quantity": "0.2"}], asks_complete=complete)
    result = costs(request([row]))
    fill = result["hypothetical_fill"]
    assert result["status"] == "unsupported"
    assert fill["gross_quantity"] == "0.2"
    assert fill["all_in_cost_quote"] == "20"
    assert fill["leftover_quote"] == "80"
    assert fill["size_relevant_depth"]["budget_supported_by_captured_levels"] is False
    assert fill["size_relevant_depth"]["fill_guaranteed"] is False
    assert "insufficient_amount_relevant_depth" in result["reasons"]


def test_partial_tail_can_cover_budget_after_quantity_rounding():
    row = snapshot(asks=[{"price": "100", "quantity": "0.15"}])
    row["assumptions"]["quantity_step"] = "0.1"
    fill = costs(request([row], budget="15"))["hypothetical_fill"]
    assert fill["gross_quantity"] == "0.1"
    assert fill["leftover_quote"] == "5"
    assert fill["size_relevant_depth"]["budget_supported_by_captured_levels"] is True


@pytest.mark.parametrize("field", ["fee_rate", "fixed_fee_quote", "payment_cost_quote", "withdrawal_cost_quote",
                                    "withdrawal_cost_base", "min_quantity", "min_notional", "fee_tier", "fee_step"])
def test_missing_fee_or_rule_never_invents_zero_or_comparable_cost(field):
    row = snapshot()
    row["assumptions"].pop(field)
    result = analyze_purchase_costs(request([row]), NOW)
    assert f"unknown_{field}" in result["rows"][0]["reasons"]
    assert result["rows"][0]["hypothetical_fill"] is None
    assert result["rows"][0]["captured_depth"]["ask_notional_quote"] == "200"
    assert result["comparability"]["all_requested_sources_supported"] is False
    assert result["comparability"]["ranking"] is None


def test_absent_assumptions_are_visible_unknowns():
    row = snapshot(assumptions=None)
    result = costs(request([row]))
    assert result["hypothetical_fill"] is None
    assert "unknown_cost_and_product_rules" in result["reasons"]


@pytest.mark.parametrize("product", [None, "instant_buy", "recurring_buy"])
def test_order_book_fees_are_not_reused_for_unknown_or_different_products(product):
    result = costs(request([snapshot(product=product)]))
    assert result["hypothetical_fill"] is None
    assert "unsupported_or_ambiguous_product" in result["reasons"]


@pytest.mark.parametrize("changes", [{"event_time": 900}, {"event_time": 900, "captured_at": 901}])
def test_fresh_capture_cannot_hide_stale_market_price(changes):
    result = costs(request([snapshot(**changes)]))
    assert result["hypothetical_fill"] is None
    assert result["timing"]["price_age_seconds"] == 100
    assert "stale_snapshot" in result["reasons"]


@pytest.mark.parametrize("changes", [{"event_time": 992}, {"captured_at": 1001}, {"event_time": 1001}])
def test_clock_skew_is_diagnostic_and_suppresses_costs(changes):
    result = costs(request([snapshot(**changes)]))
    assert result["hypothetical_fill"] is None
    assert "clock_skew" in result["reasons"]


def test_entity_service_evidence_is_independent_of_global_cost_calculation():
    evidence = {"jurisdiction": "France", "legal_entity": "Unqualified synthetic entity", "service": "spot",
                "status": "ineligible", "input_reference": "synthetic-entity-review"}
    data = request([snapshot("material_non_mica_venue", purchase_eligibility=evidence)])
    result = analyze_purchase_costs(data, NOW)
    assert result["rows"][0]["hypothetical_fill"]["quantity_received"] == "1"
    assert result["rows"][0]["purchase_eligibility"] == evidence
    assert result["comparability"]["purchase_eligibility_is_separate"] is True


def test_purchase_inter_source_skew_prevents_aligned_comparison_not_individual_arithmetic():
    result = analyze_purchase_costs(request([snapshot(), snapshot("b", event_time=980, captured_at=981)]), NOW)
    assert result["timing"]["event_skew_seconds"] == 10
    assert result["comparability"]["aligned"] is False
    assert all(row["hypothetical_fill"]["quantity_received"] == "1" for row in result["rows"])


def test_declared_price_step_does_not_round_real_levels_into_invented_prices():
    row = snapshot(asks=[{"price": "100.01", "quantity": "2"}])
    row["assumptions"]["price_step"] = "0.1"
    result = costs(request([row]))
    assert result["hypothetical_fill"] is None
    assert "ask_price_off_declared_step" in result["reasons"]


def test_fee_rounding_or_withdrawal_cannot_create_negative_received_quantity():
    row = snapshot()
    row["assumptions"].update(fee_currency="base", fee_rate="0.5", fee_step="1")
    result = costs(request([row], budget="1"))
    assert "nonpositive_received_quantity" in result["reasons"]
    assert result["hypothetical_fill"]["quantity_received"] == "0"
    assert result["hypothetical_fill"]["leftover_quote"] == "1"


def test_two_venue_range_and_spreads_have_explicit_midpoint_denominators():
    a = price(value="100", best_bid="99", best_ask="101")
    b = price("b", "110", best_bid="109", best_ask="111")
    result = analyze_venue_disagreement(diagnostic([a, b], price_basis="mid"), NOW)
    assert result["status"] == "diagnostic"
    assert result["range"]["range_quote"] == "10"
    assert result["range"]["range_bps"] == "952.380952380952380952"
    assert result["pairwise"][0]["right_minus_left_quote"] == "10"
    assert result["rows"][0]["spread_bps"] == "200"
    assert all(row["outlier"] is None for row in result["rows"])
    assert result["outlier_method"]["supported"] is False


def test_outlier_is_retained_and_stale_missing_sources_remain_visible():
    data = diagnostic([price(), price("b", "101"), price("outlier", "120"),
                       price("stale", "80", event_time=900)], sources=["a", "b", "outlier", "stale", "absent"])
    result = analyze_venue_disagreement(data, NOW)
    assert result["status"] == "diagnostic"
    assert result["range"]["minimum_price"] == "100"
    assert result["range"]["maximum_price"] == "120"  # outlier was not discarded
    assert result["rows"][2]["outlier"] is True
    assert result["rows"][2]["median_deviation_bps"] == "1881.188118811881188119"
    assert result["rows"][3]["price"] == "80"
    assert result["rows"][3]["outlier"] is None
    assert result["rows"][4]["status"] == "missing"
    assert result["coverage"]["usable_sources"] == ["a", "b", "outlier"]
    assert result["coverage"]["excluded_sources"] == ["stale", "absent"]
    assert result["timing"]["aligned"] is False  # full sample's stale event remains visible
    assert result["usable_timing"]["aligned"] is True
    assert len(result["pairwise"]) == 3


def test_outlier_threshold_is_strict_and_even_sample_uses_middle_pair():
    rows = [price("a", "99"), price("b", "100"), price("c", "100"), price("d", "101")]
    result = analyze_venue_disagreement(diagnostic(rows), NOW)
    assert [row["outlier"] for row in result["rows"]] == [False] * 4
    assert result["rows"][0]["median_deviation_bps"] == "100"


@pytest.mark.parametrize("changes", [{"event_time": 980, "captured_at": 981}, {"captured_at": 999}])
def test_either_event_or_capture_skew_suppresses_range_and_outlier_signals(changes):
    result = analyze_venue_disagreement(diagnostic([price(), price("b", "120", **changes)]), NOW)
    assert result["status"] == "unsupported"
    assert result["range"] is None
    assert result["pairwise"] == []
    assert result["reasons"] == ["inter_source_clock_skew"]
    assert all(row["outlier"] is None for row in result["rows"])


def test_future_and_stale_prices_are_visible_without_entering_diagnostic_range():
    result = analyze_venue_disagreement(diagnostic([price(), price("future", "120", captured_at=1001)]), NOW)
    assert result["rows"][1]["price"] == "120"
    assert result["rows"][1]["timing"]["clock_skew"] is True
    assert result["range"] is None
    assert result["reasons"] == ["fewer_than_two_fresh_sources"]


@pytest.mark.parametrize("basis,changes", [("mid", {"best_bid": "98", "best_ask": "100"}),
                                           ("best_ask", {"best_ask": "101"}),
                                           ("last", {"best_bid": "101", "best_ask": "99"})])
def test_ambiguous_price_basis_and_crossed_quotes_are_rejected(basis, changes):
    with pytest.raises(ValueError):
        analyze_venue_disagreement(diagnostic([price(**changes)], price_basis=basis), NOW)


def test_unknown_spread_does_not_invent_zero_and_locked_book_can_have_zero_spread():
    result = analyze_venue_disagreement(diagnostic([price(), price("b", "100", best_bid="100", best_ask="100")]), NOW)
    assert result["rows"][0]["spread_quote"] is None
    assert result["rows"][1]["spread_quote"] == "0"


@pytest.mark.parametrize("function,make", [(analyze_purchase_costs, request), (analyze_venue_disagreement, diagnostic)])
@pytest.mark.parametrize("field,value", [("quote", "USDT"), ("quote", "USDC"), ("asset", "SOL"),
                                          ("data_mode", "synthetic"), ("data_mode", None)])
def test_native_market_and_existing_modes_are_explicit(function, make, field, value):
    with pytest.raises(ValueError):
        function(make(**{field: value}), NOW)


@pytest.mark.parametrize("field,value", [("quote", "USD"), ("quote", "USDT"), ("asset", "ETH"), ("data_mode", "live")])
def test_record_identity_must_match_selected_cohort(field, value):
    data = request()
    data["snapshots"][0][field] = value
    with pytest.raises(ValueError, match="match"):
        analyze_purchase_costs(data, NOW)


def test_usd_and_eur_stay_independent_without_conversion():
    eur = analyze_purchase_costs(request(), NOW)
    usd = analyze_purchase_costs(request([snapshot(quote="USD")], quote="USD"), NOW)
    assert eur["quote"] == "EUR" and usd["quote"] == "USD"
    assert eur["rows"][0]["hypothetical_fill"] == usd["rows"][0]["hypothetical_fill"]


@pytest.mark.parametrize("bad", [True, 1.0, "NaN", "Infinity", "1e2", "-1", "0.0000000000000000001", "1000000000000000000"])
def test_money_is_exact_bounded_nonnegative_decimal_input(bad):
    with pytest.raises(ValueError):
        analyze_purchase_costs(request(budget=bad), NOW)


@pytest.mark.parametrize("field", ["quantity_step", "price_step"])
def test_zero_market_step_is_rejected_instead_of_assuming_no_rule(field):
    row = snapshot()
    row["assumptions"][field] = "0"
    with pytest.raises(ValueError):
        analyze_purchase_costs(request([row]), NOW)


@pytest.mark.parametrize("changes", [
    {"asks": [{"price": "101", "quantity": "1"}, {"price": "100", "quantity": "1"}]},
    {"asks": [{"price": "100", "quantity": "1"}, {"price": "100", "quantity": "1"}]},
    {"asks": [{"price": "100", "quantity": "0"}]},
    {"asks": [{"price": "100", "quantity": "1", "units": "quote"}]},
    {"asks_complete": None}, {"asks": None},
])
def test_actual_levels_require_ordered_positive_base_quantities(changes):
    with pytest.raises(ValueError):
        analyze_purchase_costs(request([snapshot(**changes)]), NOW)


def test_empty_book_is_visible_without_fabricated_execution():
    result = costs(request([snapshot(asks=[])]))
    assert result["hypothetical_fill"] is None
    assert result["reasons"] == ["missing_ask_levels"]


def test_individual_and_total_level_bounds():
    levels = [{"price": str(i + 1), "quantity": "1"} for i in range(500)]
    with pytest.raises(ValueError, match="500"):
        analyze_purchase_costs(request([snapshot(asks=[*levels, {"price": "501", "quantity": "1"}])]), NOW)
    rows = [snapshot(str(i), asks=levels) for i in range(4)] + [snapshot("last")]
    with pytest.raises(ValueError, match="2000 total"):
        analyze_purchase_costs(request(rows), NOW)


def test_source_and_timestamp_bounds_and_no_ambiguous_vintage_selection():
    with pytest.raises(ValueError, match="unique"):
        analyze_purchase_costs(request(sources=["a", "a"]), NOW)
    with pytest.raises(ValueError, match="Duplicate"):
        analyze_purchase_costs(request([snapshot(), snapshot()], sources=["a", "b"]), NOW)
    with pytest.raises(ValueError, match="32"):
        analyze_purchase_costs(request(sources=[str(i) for i in range(33)]), NOW)
    with pytest.raises(ValueError, match="as_of"):
        analyze_purchase_costs(request(), True)
    with pytest.raises(ValueError, match="event_time"):
        analyze_purchase_costs(request([snapshot(event_time=-1)]), NOW)
    with pytest.raises(ValueError, match="max_age"):
        analyze_purchase_costs(request(max_age_seconds=86401), NOW)


def test_missing_snapshots_have_visible_denominators_and_no_calculations():
    data = request([], sources=["a", "b"])
    result = analyze_purchase_costs(data, NOW)
    assert result["status"] == "unsupported"
    assert result["coverage"]["missing_sources"] == ["a", "b"]
    assert all(row["hypothetical_fill"] is None for row in result["rows"])
    assert result["timing"]["event_skew_seconds"] is None


@pytest.mark.parametrize("function,make", [(analyze_purchase_costs, request), (analyze_venue_disagreement, diagnostic)])
def test_inputs_are_unchanged_outputs_do_not_alias_and_decimal_context_is_local(function, make):
    data = make()
    original = deepcopy(data)
    baseline = function(data, NOW)
    with localcontext() as context:
        context.prec = 7
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert function(data, NOW) == baseline
        assert context.prec == 7 and context.traps[Inexact]
    assert data == original
    baseline["coverage"]["requested_sources"].append("mutation")
    if function is analyze_purchase_costs:
        baseline["rows"][0]["assumptions"]["fee_rate"] = "0.99"
    assert data == original


def test_source_order_is_declared_order_not_price_ranking():
    rows = [price("expensive", "120"), price("cheap", "100"), price("middle", "110")]
    result = analyze_venue_disagreement(diagnostic(rows), NOW)
    assert [row["source"] for row in result["rows"]] == ["expensive", "cheap", "middle"]
    assert "index" not in result and "recommendation" not in result


def test_maximum_precision_and_tiny_step_conserve_cash_without_float_arithmetic():
    row = snapshot(asks=[{"price": "999999999999999999.999999999999999999", "quantity": "1"}])
    row["assumptions"].update(quantity_step="0.000000000000000001", price_step="0.000000000000000001",
                              fee_rate="0.999999999999999999")
    result = costs(request([row], budget="999999999999999999.999999999999999999"))
    fill = result["hypothetical_fill"]
    assert fill["gross_quantity"] == "0.5"
    with localcontext() as context:
        context.prec = 96
        assert Decimal(fill["all_in_cost_quote"]) + Decimal(fill["leftover_quote"]) == Decimal("999999999999999999.999999999999999999")
        assert Decimal(fill["leftover_quote"]) >= 0


def test_shareable_synthetic_examples_run_with_explicit_cutoff():
    base = Path(__file__).resolve().parents[1] / "examples"
    purchase = analyze_purchase_costs(json.loads((base / "offline-purchase-costs.json").read_text()), 1793145600)
    disagreement = analyze_venue_disagreement(json.loads((base / "offline-venue-disagreement.json").read_text()), 1793145600)
    assert purchase["rows"][0]["hypothetical_fill"]["all_in_cost_quote"] == "112.1"
    assert disagreement["rows"][2]["outlier"] is True
    assert purchase["data_mode"] == disagreement["data_mode"] == "demo"
