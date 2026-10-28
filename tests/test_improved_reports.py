"""Behavioral report checks using actual public calculation outputs."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from crypto_market_analysis.context import market_context
from crypto_market_analysis.models import Candle
from crypto_market_analysis.plan_evaluation import evaluate_plans
from crypto_market_analysis.plans import compare_plans
from crypto_market_analysis.purchase_windows import resolve_window
from crypto_market_analysis.reporting import render_accumulation_report

DAY = 86400


def bars(days=400):
    return [
        Candle("fixture", "BTC", "EUR", "1d", d * DAY, (d + 1) * DAY, 10, 10, 10, 10, 1, (d + 1) * DAY)
        for d in range(-1, days)
    ]


def config():
    return {
        "schema_version": 1,
        "source": "fixture",
        "asset": "BTC",
        "quote": "EUR",
        "start_time": 0,
        "contributions": [{"time": 0, "amount": "100"}],
        "schedule": [{"time": 0, "amount": "100"}],
        "execution": {"timeframe": "1d", "max_price_age_seconds": DAY},
        "plans": {"deferred": {"fraction": "1", "decline": "0.2", "max_wait_days": 7, "fallback": "reserve"}},
    }


def test_evaluation_reports_actual_metrics_samples_exclusions_and_protocol():
    data = [replace(c, open=20, high=20, low=20, close=20) if c.open_time > 0 else c for c in bars()]
    result = evaluate_plans(config(), data, 400 * DAY, origins=[0, 90 * DAY, 180 * DAY])
    report = render_accumulation_report(result, "evaluate-plans")
    assert "Sample size: 2" in report
    assert "Underperformance count" in report
    assert "50%" in report
    assert "-100 EUR" in report
    assert "overlapping_180_day_origin" in report
    assert "externally_fixed_no_fitting_or_optimization" in report
    assert "1970-01-01T00:00:00+00:00" in report
    assert "Population standard deviation" in report


def test_plan_and_terminal_tables_are_contiguous_and_paired_only_when_comparable():
    result = compare_plans(config(), bars(), 100 * DAY)
    report = render_accumulation_report(result, "compare-plans")
    plan_header = next(
        i for i, line in enumerate(report.splitlines()) if line.startswith("| Plan | Quantity")
    )
    assert all(line.startswith("|") for line in report.splitlines()[plan_header : plan_header + 5])
    assert "Wealth difference vs regular" in report
    terminal_lines = [line for line in report.splitlines() if line.startswith("| deferred | 180 |")]
    assert len(terminal_lines) == 1 and "pending" in terminal_lines[0] and "unknown" in terminal_lines[0]
    result["plans"]["deferred"]["terminals"]["90"]["contributions"] = "999"
    report = render_accumulation_report(result, "compare-plans")
    assert "incomparable_cashflows_or_valuation" in report


def test_reports_show_funding_shortfall_waiting_expiry_and_unfilled_reason():
    cfg = config()
    cfg["fees"] = {"fixed": "1", "rate": "0", "asset": "quote"}
    waiting = render_accumulation_report(compare_plans(cfg, bars(), DAY), "plans")
    assert "Funding shortfall" in waiting and "True" in waiting
    assert "Waiting targets" in waiting and "1970-01-08T00:00:00+00:00" in waiting
    expired = render_accumulation_report(compare_plans(cfg, bars(), 10 * DAY), "plans")
    assert "Expired targets" in expired and "604800" in expired
    sparse = [c for c in bars() if c.open_time in {-DAY, 9 * DAY}]
    unfilled = render_accumulation_report(compare_plans(cfg, sparse, 10 * DAY), "plans")
    assert "Unfilled purchases" in unfilled
    assert "no_eligible_price_within_execution_window" in unfilled


def test_missing_context_retains_each_metric_coverage_and_reason():
    report = render_accumulation_report(market_context([], 10 * DAY, [7]), "context")
    assert "Complete required UTC days are missing" in report
    assert "Required days" in report and "Obtained days" in report
    assert "unknown" in report


def test_nested_issue_assessment_and_observe_reports_explain_payloads():
    plans = compare_plans(config(), bars(), 100 * DAY)
    reference = {"asset": "BTC", "quote": "EUR", "source": "fixture", "data_mode": "live", "as_of": 100 * DAY}
    issue = {
        "assessment_id": "assessment_fixture",
        "issued_at": 0,
        "payload": {
            "kind": "accumulation_assessment",
            "input_reference": reference,
            "context": market_context([], 100 * DAY, [7]),
            "plan": plans,
        },
    }
    for kind in ("issue", "assessment"):
        report = render_accumulation_report(issue, kind)
        assert "BTC/EUR" in report and "assessment_fixture" in report
        assert "Quantity acquired" in report and "Context" in report
    window = resolve_window([], 10, 0, 0, 1, 0.05, 100 * DAY)
    observed = {
        "assessment_id": "assessment_fixture",
        "observed_at": 100 * DAY,
        "payload": {
            "as_of": 100 * DAY,
            "input_reference": reference,
            "status": "observed",
            "windows": [window],
            "plan": plans,
        },
    }
    report = render_accumulation_report(observed, "observe")
    assert "ambiguous" in report and "Endpoint below anchor" in report
    assert "No known completed hourly close exists" in report
    assert "Quantity acquired" in report


def test_render_preserves_exact_json_and_native_quote_amounts():
    result = compare_plans(config(), bars(), 100 * DAY)
    result["plans"]["regular"]["reserve"] = "0.1234567890123456789012345678"
    before = json.dumps(result, sort_keys=True)
    copied = deepcopy(result)
    report = render_accumulation_report(result, "plans")
    assert "0.1234567890123456789012345678 EUR" in report
    assert result == copied and json.dumps(result, sort_keys=True) == before
    assert report == render_accumulation_report(result, "plans")


def test_example_split_and_deferred_show_distinct_declared_behavior():
    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "accumulation-plan.json").read_text()
    )
    start = cfg["start_time"]
    data = [
        Candle(
            "demo",
            "BTC",
            "EUR",
            "1h",
            start + hour * 3600,
            start + (hour + 1) * 3600,
            100,
            100,
            100,
            100,
            1,
            start + (hour + 1) * 3600,
            data_mode="demo",
        )
        for hour in range(-1, 2)
    ]
    result = compare_plans(cfg, data, start + 7200)
    assert result["plans"]["split"]["history"]
    assert result["plans"]["deferred"]["history"] == []
    report = render_accumulation_report(result, "plans")
    assert "SYNTHETIC DEMONSTRATION" in report
    assert "Waiting targets" in report


def test_brief_keeps_pending_outcomes_unknown_and_pending_target_deadlines_visible():
    comparison = compare_plans(config(), bars(), DAY)
    market = {key: comparison[key] for key in ("asset", "quote", "source", "data_mode")}
    pending = comparison["plans"]["deferred"]["waiting_targets"][0]
    result = {
        "market": market,
        "as_of": DAY,
        "current_observed": {
            "anchor": {"price": "10", "time": DAY, "age_seconds": 0, "status": "fresh"},
            "context": market_context([], DAY, [7]),
        },
        "hypothetical_policy": {
            "status": "declared",
            "current_simulation": comparison,
            "pending_targets": {
                "deferred": [{**pending, "fallback": "reserve", "interpretation": "hypothetical"}]
            },
        },
        "historical_outcomes": {
            "180": {
                "time": 180 * DAY,
                "status": "pending",
                "reason": "future_horizon",
                "comparisons": {"deferred": {"terminal_wealth_difference": "999"}},
            }
        },
    }
    report = render_accumulation_report(result, "accumulation-brief")
    assert "Current observed conditions" in report and "Hypothetical declared policy" in report
    assert "Pending policy target" in report and "1970-01-08T00:00:00+00:00" in report
    assert "future_horizon" in report and "999 EUR" not in report


def test_sensitivity_shows_case_assumptions_and_comparison_direction():
    comparison = compare_plans(config(), bars(), 100 * DAY)
    result = {
        **{key: comparison[key] for key in ("asset", "quote", "source", "data_mode", "as_of")},
        "baseline_case": "declared costs",
        "cases": [
            {
                "name": "declared costs",
                "costs": {"fees": {"rate": "0.01", "fixed": "1", "asset": "quote"}},
                "plans": comparison["plans"],
                "outcomes": {},
                "differences_from_baseline": {},
            }
        ],
        "ranking_reversals": [],
    }
    before = deepcopy(result)
    report = render_accumulation_report(result, "cost-sensitivity")
    assert "Baseline cost case: declared costs" in report and "Rate: 1%" in report
    assert "policy minus regular" in report and "current cost case minus first cost case" in report
    assert "No cost case or policy is selected as preferred" in report
    assert result == before
