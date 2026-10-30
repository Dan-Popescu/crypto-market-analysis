"""Pure scorecards use synthetic cashflows, never real performance claims."""

from copy import deepcopy
from decimal import Decimal

import pytest

from crypto_market_analysis.evidence import canonical_json, content_hash
from crypto_market_analysis.models import Candle
from crypto_market_analysis.plans import DAY, compare_plans
from crypto_market_analysis.scorecards import build_scorecard


def original(identity="a", *, start=DAY, issued=None, mode="import", quote="EUR", fee="0", amount="100",
             fixed_fee="0", fee_asset="quote", starting_quantity="0"):
    issued = start if issued is None else issued
    config = {"schema_version": 1, "source": "test", "asset": "BTC", "quote": quote,
              "data_mode": mode, "start_time": start,
              "contributions": [{"time": start, "amount": amount}],
              "schedule": [{"time": start, "amount": amount}],
              "fees": {"rate": fee, "fixed": fixed_fee, "asset": fee_asset},
              "starting_holdings": {"quantity": starting_quantity},
              "execution": {"timeframe": "1d", "max_price_age_seconds": DAY},
              "plans": {"deferred": {"fraction": "1", "decline": "0.2", "max_wait_days": 7,
                                     "fallback": "reserve"}}}
    bars = [Candle("test", "BTC", quote, "1d", start - DAY, start, 10, 10, 10, 10, 1, start,
                   data_mode=mode)]
    frozen = compare_plans(config, bars, max(issued, start))
    config = frozen["config"]
    reference = {**{key: config[key] for key in ("source", "asset", "quote", "data_mode")},
                 "as_of": issued, "available_at": issued,
                 "dataset_lineage": {"name": "synthetic-research"}, "content_hash": content_hash(bars),
                 "configuration_hash": content_hash(config)}
    frozen["input_reference"] = deepcopy(reference)
    return {"assessment_id": identity, "issued_at": issued, "content_hash": "issue-" + identity,
            "payload": {"kind": "accumulation_assessment", "input_reference": deepcopy(reference),
                        "rules": {"purchase_windows": 1},
                        "outcome_protocol": {"endpoint_convention": "hourly_after_deadline_v1"},
                        "windows": {"windows": {"1": {"0.05": {}}}}, "plan": frozen}}


def appended(record, *, days=181, rising=True, rise_after_first=False, digest="first", sparse=False):
    frozen = record["payload"]["plan"]
    config = frozen["config"]
    start = config["start_time"]
    clock = max(start + days * DAY, record["issued_at"])
    def price(day):
        return 10 if day < 0 or not rising or (rise_after_first and day == 0) else 20

    bars = [Candle(config["source"], config["asset"], config["quote"], "1d",
                   start + day * DAY, start + (day + 1) * DAY,
                   price(day), price(day), price(day), price(day),
                   1, start + (day + 1) * DAY, data_mode=config["data_mode"])
            for day in range(-1, days)]
    if sparse:
        bars = [bar for bar in bars if bar.close_time in (start, start + 90 * DAY, start + 180 * DAY)]
    result = compare_plans(config, bars, clock, validation=frozen["validation_mode"])
    result["input_reference"] = {**frozen["input_reference"], "as_of": clock,
                                 "available_at": clock, "content_hash": content_hash(bars)}
    result["replay"] = {"assessment_id": record["assessment_id"],
                        "original_content_hash": record["content_hash"],
                        "original_input_reference": deepcopy(frozen["input_reference"]),
                        "frozen_rule_version": frozen["rule_version"],
                        "pinned_candle_versions": 1, "pinned_content_hash": "pinned",
                        "future_candle_versions": len(bars) - 1, "future_content_hash": "future",
                        "future_price_selection": "first_observed_version_fully_post_issuance",
                        "boundary_policy": "exclude_bars_straddling_actual_issuance"}
    reference = record["payload"]["input_reference"]
    event = {"state": "known_true", "value": True}
    return {"assessment_id": record["assessment_id"], "observed_at": clock, "content_hash": digest,
            "payload": {"as_of": clock, "original_input_reference": deepcopy(reference),
                        "input_reference": {**reference, "as_of": clock, "available_at": clock},
                        "plan": result, "windows": [{"horizon_days": 1, "threshold": 0.05,
                            "as_of": clock, "endpoint_below_anchor": event,
                            "below_anchor_touch": event, "observable_endpoint": event}]}}


def cohort(result):
    return result["plan_cohorts"][0]


def test_rising_cashflow_pair_includes_reserve_and_known_underperformance():
    record = original()
    outcome = appended(record)
    result = build_scorecard([record], {"a": [outcome]}, outcome["observed_at"])
    plan = cohort(result)
    assert plan["identity"]["temporality"] == "prospective"
    for days in ("90", "180"):
        assert plan["counts"][days]["sample_size"] == 1
        deferred = plan["metrics"][days]["deferred"]
        assert deferred["reserve"]["mean"] == "100"
        assert deferred["total_wealth"]["mean"] == "100"
        # Immediate regular buys at 20, so a reserve-only deferred plan ties.
        assert deferred["terminal_wealth_difference"]["mean"] == "0"
        assert deferred["quantity_difference"]["mean"] == "-5"
        assert deferred["new_purchases_all_in_cost"]["sample_size"] == 0
        assert deferred["acquisition_cost_difference"]["mean"] is None
        assert deferred["cash_waiting_quote_days"]["mean"] == "700"
        assert deferred["cash_weighted_waiting_seconds"]["mean"] == "604800"
    assert canonical_json(result)


def test_known_rising_after_regular_fill_reports_a_loss_including_reserve():
    record = original()
    outcome = appended(record, rise_after_first=True)
    result = build_scorecard([record], {"a": [outcome]}, outcome["observed_at"])
    metric = cohort(result)["metrics"]["180"]["deferred"]
    assert metric["terminal_wealth_difference"]["mean"] == "-100"
    assert metric["underperformance"]["fraction"] == "1"
    assert Decimal(metric["underperformance"]["wilson_95"]["lower"]) > 0


@pytest.mark.parametrize("fee_asset", ["quote", "base"])
def test_actual_ledger_costs_and_starting_holdings_are_paired_without_known_initial_cost(fee_asset):
    record = original(fee="0.01", fixed_fee="0.1", fee_asset=fee_asset, starting_quantity="2")
    outcome = appended(record, rise_after_first=True)
    result = build_scorecard([record], {"a": [outcome]}, outcome["observed_at"])
    plan = cohort(result)
    assert plan["counts"]["180"]["sample_size"] == 1
    terminals = plan["scenarios"][0]["horizons"]["180"]["plans"]
    assert Decimal(terminals["deferred"]["total_wealth"]) == 140
    assert Decimal(terminals["regular"]["holdings_quantity"]) > 2
    assert terminals["regular"]["new_purchases_all_in_cost"] is not None
    assert plan["metrics"]["180"]["deferred"]["terminal_wealth_difference"]["maximum"] is not None


def test_first_complete_observed_scenario_pins_even_unqualified_execution():
    record = original()
    sparse = appended(record, sparse=True, digest="sparse")
    complete = appended(record, days=182, digest="later")
    result = build_scorecard([record, record], {"a": [complete, sparse, sparse]}, complete["observed_at"])
    assert result["duplicate_assessments"] == 1
    plan = cohort(result)
    assert plan["counts"]["180"]["scenario_sample_size"] == 1
    assert plan["counts"]["180"]["sample_size"] == 0
    assert plan["counts"]["180"]["unqualified_scenarios"] == 1
    horizon = plan["scenarios"][0]["horizons"]["180"]
    assert horizon["outcome_hash"] == "sparse"
    assert horizon["qualification_reasons"] == ["funded_execution_evidence_unqualified"]


def test_pending_missing_and_cutoff_remain_honest_and_future_appends_are_ignored():
    record = original()
    outcome = appended(record)
    result = build_scorecard([record], {"a": [outcome]}, record["issued_at"] + 100 * DAY)
    plan = cohort(result)
    assert plan["counts"]["90"]["missing"] == 1
    assert plan["counts"]["180"]["pending"] == 1
    assert plan["counts"]["90"]["sample_size"] == 0
    assert not build_scorecard([record], {}, record["issued_at"] - 1)["plan_cohorts"]


def test_first_definite_result_is_not_rewritten_by_later_revision():
    record = original()
    first = appended(record)
    revision = appended(record, days=182, rising=False, digest="revision")
    result = build_scorecard([record], {"a": [revision, first]}, revision["observed_at"])
    assert cohort(result)["scenarios"][0]["horizons"]["180"]["outcome_hash"] == "first"
    assert all(row["events"][0]["outcome_hash"] == "first" for row in result["event_cohorts"])


@pytest.mark.parametrize("change", ["fee", "cashflow", "rule", "validation", "replay", "lineage", "original", "wealth", "cost", "outcome_identity", "cutoff", "availability"])
def test_changed_frozen_policy_lineage_replay_and_bad_accounting_are_rejected(change):
    record = original()
    bad = appended(record, digest="bad")
    plan = bad["payload"]["plan"]
    if change == "fee":
        plan["config"]["fees"]["rate"] = "0.01"
    elif change == "cashflow":
        plan["config"]["contributions"][0]["amount"] = "200"
    elif change == "rule":
        plan["rule_version"] = "unknown"
    elif change == "validation":
        plan["validation_mode"] = "known_at"
    elif change == "replay":
        plan["replay"]["original_content_hash"] = "foreign"
    elif change == "lineage":
        plan["input_reference"]["dataset_lineage"] = {"name": "other"}
    elif change == "original":
        bad["payload"]["original_input_reference"] = {}
    elif change == "wealth":
        plan["plans"]["deferred"]["terminals"]["180"]["total_wealth"] = "200"
    elif change == "cost":
        plan["plans"]["regular"]["terminals"]["180"]["new_purchases_all_in_cost"] = "0.01"
    elif change == "outcome_identity":
        bad["assessment_id"] = "other"
    elif change == "cutoff":
        bad["payload"]["as_of"] = bad["observed_at"] + 1
    else:
        plan["input_reference"]["available_at"] = bad["observed_at"] + 1
    result = build_scorecard([record], {"a": [bad]}, bad["observed_at"])
    horizon = cohort(result)["scenarios"][0]["horizons"]["180"]
    assert horizon["status"] == "missing"
    assert horizon["rejected_outcomes"] or cohort(result)["scenarios"][0]["rejected_outcomes"]


def test_chronology_modes_and_temporal_plan_classes_do_not_mix():
    records = [original("prospective"), original("hybrid", issued=100 * DAY),
               original("retrospective", issued=200 * DAY), original("demo", mode="demo")]
    result = build_scorecard(records, {row["assessment_id"]: [appended(row)] for row in records}, 250 * DAY)
    assert {row["identity"]["temporality"] for row in result["plan_cohorts"]} == {
        "prospective", "retrospective", "hybrid", "unverified"}
    demo = next(row for row in result["plan_cohorts"] if row["identity"]["data_mode"] == "demo")
    assert demo["counts"]["180"]["scenario_sample_size"] == 1
    assert demo["counts"]["180"]["sample_size"] == 0
    assert "non_market_data_mode" in demo["scenarios"][0]["exclusions"]


def test_native_market_rules_fees_cashflows_and_lineage_separate_cohorts():
    records = [original("base"), original("usd", quote="USD"), original("fee", fee="0.01"),
               original("cashflow", amount="200"), original("live", mode="live"), original("lineage"),
               original("rules")]
    records[-2]["payload"]["input_reference"]["dataset_lineage"]["name"] = "other"
    records[-2]["payload"]["plan"]["input_reference"]["dataset_lineage"]["name"] = "other"
    records[-1]["payload"]["rules"]["purchase_windows"] = 2
    result = build_scorecard(records, {}, 200 * DAY)
    assert len(result["plan_cohorts"]) == len(records)


def test_relative_cashflows_pool_different_starts_and_select_before_outcomes():
    records = [original("first"), original("overlap", start=91 * DAY), original("next", start=181 * DAY)]
    outcomes = {row["assessment_id"]: [appended(row)] for row in records[1:]}
    result = build_scorecard(records, outcomes, 400 * DAY)
    assert len(result["plan_cohorts"]) == 1
    plan = cohort(result)
    assert [(row["assessment_id"], row["independent_selection"]) for row in plan["scenarios"]] == [
        ("first", True), ("overlap", False), ("next", True)]
    assert plan["counts"]["180"] == {"issued": 3, "mature": 3, "complete": 2, "missing": 1,
        "pending": 0, "excluded": 0, "scenario_sample_size": 2,
        "qualified_before_overlap": 2, "sample_size": 1, "overlap_omitted": 1,
        "unqualified_scenarios": 0, "selected": 2, "selected_missing": 1, "selected_pending": 0}
    assert plan["selection"] == {"selected_assessment_ids": ["first", "next"],
                                  "overlap_omitted_assessment_ids": ["overlap"]}
    assert plan["scenarios"][1]["overlap_with"] == "first"


def test_event_rates_exclude_overlap_without_dropping_missing_denominators_or_scoring_forecasts():
    records = [original("first"), original("overlap", issued=DAY + 1), original("next", start=2 * DAY)]
    outcomes = {row["assessment_id"]: [appended(row)] for row in records[1:]}
    result = build_scorecard(records, outcomes, 200 * DAY)
    for event in result["event_cohorts"]:
        assert event["counts"]["issued"] == 3
        assert event["counts"]["missing"] == 1
        assert event["descriptive_rate"]["sample_size"] == 1
        assert event["independent_denominators"]["selected"] == 2
        assert event["independent_denominators"]["mature"] == 2
        assert event["probability_scoring"] == {"status": "comparability_not_established",
                                                "brier_score": None, "calibration": None}


def test_no_mutation_and_bounds():
    records = [original(str(index)) for index in range(101)]
    outcome = appended(records[0])
    outcomes = {"0": [outcome] * 257}
    before = canonical_json([records, outcomes])
    result = build_scorecard(records, outcomes, 200 * DAY)
    assert result["assessment_count"] == 100
    assert result["truncation"] == {"assessments_omitted": 1, "outcomes_omitted": {"0": 1}}
    assert canonical_json([records, outcomes]) == before


def test_bad_original_and_unverified_availability_are_explicitly_excluded():
    record = original()
    outcome = appended(record)
    del record["payload"]["plan"]["input_reference"]["available_at"]
    result = build_scorecard([record], {"a": [outcome]}, outcome["observed_at"])
    assert cohort(result)["counts"]["180"]["excluded"] == 1
    assert cohort(result)["counts"]["180"]["sample_size"] == 0
    record["payload"]["plan"]["config"] = {"schema_version": 1}
    result = build_scorecard([record], {}, 200 * DAY)
    assert result["excluded_plans"][0]["reasons"] == ["invalid_frozen_config"]


@pytest.mark.parametrize("as_of", [True, -1, "200000"])
def test_invalid_clock(as_of):
    with pytest.raises(ValueError, match="as_of"):
        build_scorecard([], {}, as_of)
