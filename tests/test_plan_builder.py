"""Constructor dates and funding constraints have no price/provider dependency."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from crypto_market_analysis.plan_builder import construct_plan, preview_plan_funding


def request(**overrides):
    return {"asset": "BTC", "quote": "EUR", "source": "reference", "data_mode": "demo",
            "start_date": "2026-01-31T00:00:00Z", "recurrence": {"frequency": "monthly", "count": 3, "month_end_rule": "clip"},
            "contribution_amount": "100.00", "purchase_amount": "100.00", **overrides}


def dates(config):
    return [datetime.fromtimestamp(row["time"], UTC).isoformat() for row in config["schedule"]]


def test_monthly_clip_preserves_original_day_and_distinct_defaults():
    raw = request()
    original = deepcopy(raw)
    config = construct_plan(raw)
    assert dates(config) == ["2026-01-31T00:00:00+00:00", "2026-02-28T00:00:00+00:00", "2026-03-31T00:00:00+00:00"]
    assert raw == original
    assert config["contributions"][0]["amount"] == "100"
    assert config["plans"]["split"]["initial_fraction"] == "0.5"
    assert config["plans"]["deferred"]["fraction"] == "1"
    assert not preview_plan_funding(config, config["start_time"])["policies_equivalent"]


def test_monthly_skip_and_daily_weekly_boundaries():
    config = construct_plan(request(recurrence={"frequency": "monthly", "count": 3, "month_end_rule": "skip"}))
    assert [date[:10] for date in dates(config)] == ["2026-01-31", "2026-03-31", "2026-05-31"]
    daily = construct_plan(request(recurrence={"frequency": "daily", "count": 181}))
    assert daily["schedule"][-1]["time"] - daily["start_time"] == 180 * 86400
    weekly = construct_plan(request(recurrence={"frequency": "weekly", "count": 26}))
    assert weekly["schedule"][-1]["time"] - weekly["start_time"] == 175 * 86400
    with pytest.raises(ValueError, match="180 days"):
        construct_plan(request(recurrence={"frequency": "weekly", "count": 27}))


@pytest.mark.parametrize("date", ["2026-01-31", "2026-01-31T00:00:00", "2026-01-31T00:00:00+01:00", "2026-01-31T00:00:00.5Z", True])
def test_constructor_requires_explicit_whole_second_utc(date):
    with pytest.raises(ValueError):
        construct_plan(request(start_date=date))


@pytest.mark.parametrize("changes", [
    {"recurrence": {"frequency": "monthly", "count": 2}},
    {"recurrence": {"frequency": "monthly", "count": True, "month_end_rule": "clip"}},
    {"recurrence": {"frequency": "monthly", "count": 8, "month_end_rule": "clip"}},
    {"purchase_amount": 100.0}, {"contribution_amount": "NaN"}, {"quote": "USDT"},
    {"unexpected": "ignored"},
])
def test_constructor_rejects_ambiguous_or_unsupported_inputs(changes):
    with pytest.raises(ValueError):
        construct_plan(request(**changes))


def test_quote_fees_prevent_full_notional_and_split_incurs_two_fixed_fees():
    config = construct_plan(request(recurrence={"frequency": "daily", "count": 1},
                                    fees={"rate": "0.01", "fixed": "1", "asset": "quote"}))
    preview = preview_plan_funding(config, config["start_time"])
    regular = preview["families"]["regular"]["tranches"][0]
    split = preview["families"]["split"]["tranches"][0]
    assert regular["allocations"][0]["required_cash"] == "102"
    assert regular["allocations"][0]["allocated_cash"] == "100"
    assert regular["funding_shortfall"]
    assert [row["required_cash"] for row in split["allocations"]] == ["51.5", "51.5"]
    assert [row["allocated_cash"] for row in split["allocations"]] == ["51.5", "48.5"]
    assert preview["families"]["split"]["current"] == {"contributions_available": "100", "free_cash": "0",
        "locked_cash": "48.5", "reserved_immediate_cash": "51.5"}


def test_future_deposits_cannot_fund_earlier_tranches():
    config = construct_plan(request(recurrence={"frequency": "daily", "count": 2}))
    start = config["start_time"]
    config["contributions"] = [{"time": start + 86400, "amount": "200"}]
    preview = preview_plan_funding(config, start)
    rows = preview["families"]["regular"]["tranches"]
    assert rows[0]["cash_available_before"] == "0"
    assert rows[0]["allocations"][0]["allocated_cash"] == "0"
    assert rows[1]["allocations"][0]["allocated_cash"] == "100"
    assert preview["families"]["regular"]["current"]["contributions_available"] == "0"


def test_reserve_fallback_releases_target_cash_and_cap_overflow_is_visible():
    config = construct_plan(request(recurrence={"frequency": "weekly", "count": 2},
        plans={"deferred": {"fraction": "1", "max_wait_days": 1, "fallback": "reserve",
                            "max_deferred_reserve_quote": "60"}}))
    start = config["start_time"]
    preview = preview_plan_funding(config, start + 86400)
    deferred = preview["families"]["deferred"]
    first = deferred["tranches"][0]["allocations"][0]
    assert first["locked_cash"] == "60"
    assert first["cap_overflow_immediate_cash"] == "40"
    assert deferred["current"]["free_cash"] == "60"
    assert deferred["current"]["locked_cash"] == "0"
    assert deferred["current"]["reserved_immediate_cash"] == "40"


def test_equivalent_policies_flag_and_base_fee_not_a_quote_cash_requirement():
    config = construct_plan(request(plans={}, fees={"asset": "base", "rate": "0.01", "fixed": "0.001"}))
    preview = preview_plan_funding(config, config["start_time"])
    assert preview["policies_equivalent"]
    assert preview["families"]["regular"]["tranches"][0]["allocations"][0]["required_cash"] == "100"
    with pytest.raises(ValueError):
        preview_plan_funding(config, True)
