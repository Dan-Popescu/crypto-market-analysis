"""Third-asset native analysis without inferred sources, quotes or availability."""

from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_market_analysis.book_snapshots import retain_books, retained_book
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.history_backfill import normalize_backfill_request
from crypto_market_analysis.local_workflow import normalize_local_workflow_request, run_local_workflow
from crypto_market_analysis.models import Candle, Observation
from crypto_market_analysis.plan_builder import construct_plan
from crypto_market_analysis.plans import compare_plans
from crypto_market_analysis.qualification import normalize_qualification_request, qualify_sources
from crypto_market_analysis.stored_analysis import (
    analyze_stored_derivatives,
    analyze_stored_quotes,
    analyze_stored_venues,
)

NOW = 4 * 86400


def candle(*, quote="EUR", **changes):
    return replace(Candle("fixture", "SOL", quote, "1h", NOW - 3600, NOW,
                          10, 10, 10, 10, 1, NOW, data_mode="import"), **changes)


def plan(quote="EUR", fees=None):
    return construct_plan({
        "asset": "SOL", "quote": quote, "source": "fixture", "data_mode": "import",
        "start_date": "1970-01-05T00:00:00Z", "recurrence": {"frequency": "daily", "count": 1},
        "contribution_amount": "100", "purchase_amount": "90", "fees": fees or {},
    })


def test_sol_public_engine_brief_and_metrics_preserve_native_known_at_dimensions(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path), "import")
    native = candle()
    engine.store.put_candles([
        native, candle(quote="USD", open=20, high=20, low=20, close=20),
        replace(native, observed_at=NOW + 1, open=30, high=30, low=30, close=30),
        replace(native, source="other"), replace(native, asset="BTC"), replace(native, data_mode="live"),
    ])
    observation = Observation("fixture", "SOL", "spot_price", NOW, NOW, 10, "EUR", "EUR", data_mode="import")
    engine.store.put_observations([observation, replace(observation, observed_at=NOW + 1, value=999),
                                   replace(observation, quote="USD", unit="USD", value=20)])
    selected, _ = engine.selected_candles("SOL", "fixture", "EUR", NOW)
    assert selected == [native]
    captured = engine.capture_inputs("SOL", "fixture", "EUR", NOW, profile="candles")
    assert captured.candles == (native,)
    brief = engine.accumulation_brief("SOL", "fixture", "EUR", NOW)
    assert brief["market"] == {"asset": "SOL", "quote": "EUR", "source": "fixture", "data_mode": "import"}
    assert brief["current_observed"]["anchor"]["price"] == "10"
    bundle = engine.bundle("SOL", "fixture", "EUR", NOW, include_forecast=False)
    assert bundle["asset"] == "SOL" and bundle["selected_market"]["quote"] == "EUR"
    assert {row["asset"] for row in bundle["latest_metrics"]} == {"SOL"}
    assert all(row["metric"] == "spot_price" for row in bundle["latest_metrics"])
    history = engine.store.metric_history("SOL", "spot_price", source="fixture", mode="import",
                                          quote="EUR", as_of=NOW)
    assert [row["value"] for row in history["points"]] == [10]
    assert engine.accumulation_brief("SOL", "fixture", "USD", NOW)["current_observed"]["anchor"]["price"] == "20"
    empty = engine.bundle("XRP", "fixture", "EUR", NOW, include_forecast=False)
    assert empty["selected_market"] is None and empty["latest_metrics"] == []


@pytest.mark.parametrize("quote", ["EUR", "USD"])
@pytest.mark.parametrize("fees", [{"asset": "quote", "fixed": "1"}, {"asset": "base", "fixed": "0.1"}])
def test_sol_plans_conserve_cash_and_base_fees_and_keep_earliest_timely_prices(quote, fees):
    cfg = plan(quote, fees)
    anchor = candle(quote=quote)
    execution = replace(anchor, open_time=NOW, close_time=NOW + 3600, observed_at=NOW + 3600)
    revisions = [replace(row, open=50, high=50, low=50, close=50, observed_at=NOW + 7200)
                 for row in (anchor, execution)]
    result = compare_plans(cfg, [anchor, execution, *revisions], NOW + 10800, validation="known_at")
    regular = result["plans"]["regular"]
    assert Decimal(regular["amount_spent"]) + Decimal(regular["fees_quote"]) + Decimal(regular["reserve"]) == 100
    assert regular["net_quantity"] == ("9" if fees["asset"] == "quote" else "8.9")
    assert regular["fees_base"] == ("0" if fees["asset"] == "quote" else "0.1")
    assert regular["history"][0]["reference_open"] == "10"
    assert result == compare_plans(cfg, [anchor, execution], NOW + 10800, validation="known_at")
    with pytest.raises(ValueError, match="identity"):
        compare_plans(cfg, [anchor, execution, replace(anchor, quote="USD" if quote == "EUR" else "EUR")], NOW + 10800)


def test_sol_retained_books_stored_costs_and_missing_assumptions_remain_native(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path), "import")
    def book(quote="EUR", at=NOW, mode="import"):
        return retained_book({"timestamp": at * 1000, "bids": [["9", "20"]], "asks": [["10", "20"]]},
                             source="fixture", asset="SOL", quote=quote, symbol=f"SOL/{quote}",
                             captured_at=at, requested_limit=100, data_mode=mode)
    native = book()
    retain_books(engine.store, [native, book("USD"), book(at=NOW + 1), book(mode="live")])
    req = {"analysis": "purchase_costs", "asset": "SOL", "quote": "EUR", "data_mode": "import",
           "sources": ["fixture"], "budget": "100"}
    unknown = analyze_stored_venues(engine.store, req, NOW)
    assert unknown["rows"][0]["hypothetical_fill"] is None
    assert unknown["stored_inputs"]["input_references"] == [native["input_reference"]]
    assumptions = {"input_reference": "synthetic-fees", "fee_tier": "synthetic", "fee_rate": "0.01",
                   "fee_currency": "quote", "fee_step": "0", "fixed_fee_quote": "0",
                   "payment_cost_quote": "0", "withdrawal_cost_quote": "0", "withdrawal_cost_base": "0",
                   "quantity_step": "0.01", "price_step": "0.01", "min_quantity": "0", "min_notional": "0"}
    cost = analyze_stored_venues(engine.store, {**req, "assumptions_by_source": {"fixture": assumptions},
                                               "product_by_source": {"fixture": "spot_order_book"}}, NOW)
    fill = cost["rows"][0]["hypothetical_fill"]
    assert Decimal(fill["all_in_cost_quote"]) + Decimal(fill["leftover_quote"]) == 100
    assert fill["quantity_received"] == "9.9"
    assert cost["asset"] == "SOL" and cost["quote"] == "EUR"
    quote_context = analyze_stored_quotes(engine.store, {"asset": "SOL", "quote": "EUR",
        "sources": ["fixture"], "data_mode": "import"}, NOW)
    assert quote_context["fx_reference"]["status"] == "missing"
    derivatives = analyze_stored_derivatives(engine.store, {"asset": "SOL", "source": "fixture", "quote": "USD",
                                                           "data_mode": "import"}, NOW)
    assert derivatives["series"] == []


def test_generic_qualification_and_workflow_stay_bounded_and_coverage_is_not_support(tmp_path):
    settings = Settings(data_dir=tmp_path, venues=["fixture"])
    engine = Engine(settings, "import")
    rows = [replace(candle(), open_time=NOW - (24 - hour) * 3600,
                    close_time=NOW - (23 - hour) * 3600) for hour in range(24)]
    engine.store.put_candles(rows)
    req = {"markets": ["SOL/EUR", "SOL/USD"], "sources": ["fixture"], "data_mode": "import",
           "required_history_days": 1}
    result = qualify_sources(engine.store, req, NOW)
    assert [row["status"] for row in result["markets"]] == ["pass", "unavailable"]
    assert all(row["capability"]["native_market_support"] == "unverified_by_documentation" for row in result["markets"])
    assert all(row["source_access"]["status"] == "untested" for row in result["markets"])
    workflow = run_local_workflow(settings, {**req, "as_of": NOW})
    assert workflow["markets"][0]["accumulation_brief"]["market"]["asset"] == "SOL"
    assert workflow["markets"][0]["analyses"]["derivatives"]["series"] == []
    for markets in (["SOL/EUR"] * 2, ["SOL/USDT"], ["SOL/EUR", "SOL/USD", "XRP/EUR", "ADA/EUR", "DOGE/EUR"]):
        with pytest.raises(ValueError):
            normalize_qualification_request({**req, "markets": markets}, NOW)
        with pytest.raises(ValueError):
            normalize_local_workflow_request(settings, {**req, "markets": markets})
    backfill = normalize_backfill_request({"source": "coinbase", "markets": [{"asset": "SOL", "quote": "EUR"}],
                                           "start": NOW - 3600, "end": NOW})
    assert backfill["markets"] == [{"asset": "SOL", "quote": "EUR"}]


def test_sol_stored_derivatives_use_explicit_native_units_and_known_at_endpoints(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path), "import")
    def point(at, value, *, unit="SOL", metric="open_interest_amount", **changes):
        return replace(Observation("fixture", "SOL", metric, at, at, value, unit, "USDT", {
            "symbol": "SOL/USDT:USDT", "settle": "USDT", "market_type": "swap",
            "counting_convention": "one_sided", "value_currency": unit,
        }, "import"), **changes)
    start, end = point(NOW - 14400, 100), point(NOW, 120)
    late = point(NOW - 14400, 999, observed_at=NOW - 14399)
    engine.store.put_observations([start, end, late, point(NOW, 999, observed_at=NOW + 1),
                                   point(NOW, 1, unit="USD"), point(NOW, 999, data_mode="live")])
    request = {"asset": "SOL", "source": "fixture", "quote": "USDT", "data_mode": "import"}
    result = analyze_stored_derivatives(engine.store, request, NOW)
    assert len(result["series"]) == 2
    sol = next(row for row in result["series"] if row["current"]["unit"] == "SOL")
    assert sol["comparisons"]["4h"]["native_change"] == 20
    assert sol["comparisons"]["4h"]["change_unit"] == "SOL"
    assert sol["comparisons"]["24h"]["native_change"] is None
    usd = next(row for row in result["series"] if row["current"]["unit"] == "USD")
    assert usd["comparisons"]["4h"]["native_change"] is None
