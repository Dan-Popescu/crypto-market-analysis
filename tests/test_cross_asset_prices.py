"""Offline financial contract cases; every observation is synthetic."""

import copy
import math

import pytest

from crypto_market_analysis.cross_asset.prices import actions_as_of, bars_as_of, validate_series
from crypto_market_analysis.cross_asset.research import analyze_series, simulate_cash_plan
from crypto_market_analysis.evidence import content_hash


def bar(start, end, close, observed_at=None):
    return {"start": start, "end": end, "open": close, "high": close, "low": close, "close": close,
            "observed_at": end if observed_at is None else observed_at}


def series(prices=(100, 110, 120), *, basis="raw", form_interval="session", revision="point_in_time"):
    return {
        "schema": "market-series.v1",
        "descriptor": {
            "id": "series:test", "instrument_id": "instrument:test", "listing_id": "listing:test",
            "provider": "synthetic", "provider_symbol": "TEST", "native_currency": "USD", "unit": "share",
            "price_basis": basis, "data_mode": "demo", "calendar_id": "calendar:test",
            "calendar_version": "v1", "interval": form_interval, "revision_semantics": revision,
        },
        "bars": [bar(i * 10, (i + 1) * 10, price) for i, price in enumerate(prices)], "actions": [],
    }


INSTRUMENT = {"id": "instrument:test", "asset_class": "equity", "form": "share"}


def plan(*, purchases=None, initial_cash=100, fee=0, complete=False):
    return {"currency": "USD", "initial_cash": initial_cash, "deposits": [],
            "purchases": [{"at": 10, "cash_amount": 100}] if purchases is None else purchases,
            "fee_per_trade": fee, "distributions": "cash", "actions_complete": complete}


def simulate(document, request=None, as_of=30):
    return simulate_cash_plan(document, plan() if request is None else request, as_of=as_of, instrument=INSTRUMENT)


def split(*, at=15, observed=14, ratio=2):
    return {"id": "split:test", "kind": "split", "effective_at": at, "announced_at": 1,
            "observed_at": observed, "ratio": ratio}


def dividend(*, at=25, pay=30, observed=24, amount=1, currency="USD"):
    return {"id": "dividend:test", "kind": "cash_dividend", "effective_at": at,
            "announced_at": 1, "observed_at": observed, "amount": amount, "currency": currency, "pay_at": pay}


def with_receipts(document, *, received_at=100, witnessed=True):
    document = copy.deepcopy(document)
    document["storage_provenance"] = {
        "descriptor_hash": content_hash(document["descriptor"]), "registry_hash": "a" * 64,
        "receipt_evidence": "locally_witnessed" if witnessed else "self_declared_history",
        **{field: [{"hash": content_hash(row), "declared_observed_at": row["observed_at"],
                    "received_at": received_at,
                    "effective_known_at": max(received_at, row["observed_at"]) if witnessed else row["observed_at"]}
                   for row in document[field]] for field in ("bars", "actions")},
    }
    return document


def test_price_revisions_use_receipt_and_closed_interval_not_input_order():
    document = series()
    correction = bar(0, 10, 200, observed_at=40)
    document["bars"] = [correction, *reversed(document["bars"]), document["bars"][0]]
    normalized = validate_series(document)
    assert len(normalized["bars"]) == 4
    assert bars_as_of(normalized, 30)[0]["close"] == 100
    assert bars_as_of(normalized, 40)[0]["close"] == 200
    assert len(bars_as_of(normalized, 19)) == 1
    conflicting = copy.deepcopy(correction)
    conflicting["close"] = conflicting["high"] = 201
    with pytest.raises(ValueError, match="conflicting"):
        validate_series({**document, "bars": document["bars"] + [conflicting]})


@pytest.mark.parametrize("value", [True, 1.5, "30", None, 253402300800, -62135596801])
def test_epoch_boundaries_reject_coercion(value):
    with pytest.raises(ValueError, match="epoch"):
        analyze_series(series(), as_of=value)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "10", 10**1000])
def test_price_numbers_must_be_finite_and_not_boolean(value):
    document = series()
    document["bars"][0]["close"] = value
    with pytest.raises(ValueError, match="finite number"):
        validate_series(document)


def test_signed_epochs_native_bounds_no_whole_hour_assumption():
    document = series()
    document["bars"] = [bar(-60, -1, 2), bar(0, 17, 3)]
    assert analyze_series(document, as_of=17)["simple_return"] == 0.5
    assert len(bars_as_of(document, 17, start=0, end=17)) == 1
    with pytest.raises(ValueError, match="overlap"):
        validate_series({**document, "bars": [bar(0, 20, 3), bar(10, 30, 4)]})


def test_unknown_fields_and_invalid_ohlc_fail_explicitly():
    document = series()
    document["descriptor"]["quote"] = "USD"
    with pytest.raises(ValueError, match="unknown fields"):
        validate_series(document)
    document = series()
    document["bars"][0]["low"] = 101
    with pytest.raises(ValueError, match="OHLC"):
        validate_series(document)
    document = series()
    document["bars"][0]["observed_at"] = 9
    with pytest.raises(ValueError, match="before its end"):
        validate_series(document)


def test_nonpositive_native_prices_have_absolute_but_no_relative_methods():
    document = series((10, 0, -37.63), form_interval="reference")
    result = analyze_series(document, as_of=30)
    assert result["absolute_change"] == pytest.approx(-47.63)
    assert result["simple_return"] is None
    assert result["log_return"] is None
    assert result["maximum_drawdown"] is None
    assert result["capabilities"]["simple_returns"] is False
    with pytest.raises(ValueError, match="raw traded"):
        simulate(document)


def test_annualization_is_explicit_not_365_or_252_default():
    ordinary = analyze_series(series(), as_of=30)
    declared = analyze_series(series(), as_of=30,
                              annualization={"periods_per_year": 200, "convention": "synthetic sessions"})
    assert ordinary["annualized_volatility"] is None
    assert declared["annualized_volatility"] == pytest.approx(ordinary["return_volatility"] * math.sqrt(200))
    assert ordinary["interpretation"] == "price_only"
    with pytest.raises(ValueError, match="object"):
        analyze_series(series(), as_of=30, annualization=252)


def test_captured_adjusted_history_is_not_historical_point_in_time_evidence():
    document = series(basis="total_return", revision="captured_history")
    result = analyze_series(document, as_of=30)
    assert result["evidence"] == "captured_history_not_historical_point_in_time"
    assert result["interpretation"] == "price_only"
    assert any("unverified" in text for text in result["limitations"])
    with pytest.raises(ValueError, match="raw traded"):
        simulate(document)


def test_split_invariance_for_raw_cash_funded_holdings():
    unsplit = simulate(series((100, 110, 120)))
    document = series((100, 55, 60))
    document["actions"] = [split()]
    result = simulate(document)
    assert result["units"] == 2
    assert result["cash_wealth"] == unsplit["cash_wealth"] == 120
    assert result["action_coverage"] == "unknown_or_incomplete"


def test_dividend_entitlement_payment_and_source_declared_completeness():
    document = series((100, 50, 49))
    document["actions"] = [split(), dividend()]
    document["descriptor"]["action_coverage"] = {
        "start": 0, "end": 40, "kinds": ["split", "cash_dividend"], "status": "declared_complete",
        "known_at": 30, "source_url": "https://example.test/actions",
    }
    pending = simulate(document, plan(complete=True), as_of=29)
    assert pending["dividends_paid"] == 0
    assert pending["dividend_receivables"] == 2
    assert pending["action_coverage"] == "unknown_or_incomplete"
    paid = simulate(document, plan(complete=True))
    assert paid["cash"] == 2
    assert paid["position_value"] == 98
    assert paid["cash_wealth"] == 100
    assert paid["action_coverage"] == "source_declared_complete"
    assert paid["dividend_receivables"] == 0


def test_purchase_at_entitlement_instant_does_not_receive_dividend():
    document = series((100, 100, 100))
    document["actions"] = [dividend(at=20, observed=19)]
    result = simulate(document, plan(purchases=[{"at": 20, "cash_amount": 100}]))
    assert result["units"] == 1
    assert result["dividends_paid"] == 0


def test_delayed_action_and_revision_never_leak_into_earlier_as_of():
    document = series((100, 50, 50))
    document["actions"] = [split(observed=29), {**split(observed=29), "observed_at": 40, "ratio": 4}]
    assert actions_as_of(document, 28) == []
    assert actions_as_of(document, 30)[0]["ratio"] == 2
    assert simulate(document, as_of=28)["units"] == 1
    with pytest.raises(ValueError, match="late-known"):
        simulate(document, as_of=30)


def test_purchases_use_originally_available_vintage_and_exact_closed_end():
    document = series()
    document["bars"].append(bar(0, 10, 200, observed_at=25))
    result = simulate(document)
    assert result["fills"][0]["price"] == 100
    assert result["units"] == 1
    with pytest.raises(ValueError, match="closed bar end"):
        simulate(document, plan(purchases=[{"at": 5, "cash_amount": 100}]))
    delayed = series()
    delayed["bars"][0]["observed_at"] = 11
    with pytest.raises(ValueError, match="already received"):
        simulate(delayed)


def test_live_receipt_provenance_cannot_be_replayed_at_earlier_cutoff():
    document = with_receipts(series(), received_at=40)
    assert bars_as_of(document, 30) == []
    assert len(bars_as_of(document, 40)) == 3
    with pytest.raises(ValueError, match="already received"):
        simulate(document, as_of=40)
    assert validate_series(validate_series(document)) == validate_series(document)
    bad = copy.deepcopy(document)
    bad["storage_provenance"]["bars"][0]["effective_known_at"] = 10
    with pytest.raises(ValueError, match="applicable receipt"):
        validate_series(bad)


def test_import_receipt_is_explicitly_hindsight_even_with_declared_pit_descriptor():
    document = with_receipts(series(), witnessed=False)
    assert len(bars_as_of(document, 30)) == 3
    assert analyze_series(document, as_of=30)["evidence"] == "captured_history_not_historical_point_in_time"
    assert simulate(document)["evidence"] == "captured_history_not_historical_point_in_time"
    document["bars"][0]["close"] = 999
    with pytest.raises(ValueError, match="OHLC|original rows"):
        validate_series(document)


def test_cashplan_costs_funding_and_currency_are_explicit():
    request = plan(initial_cash=101, fee=1)
    result = simulate(series(), request)
    assert result["cash"] == 0
    assert result["fees"] == {"status": "explicit", "paid": 1}
    assert simulate(series(), plan(fee=None))["fees"] == {"status": "unknown_excluded", "paid": None}
    with pytest.raises(ValueError, match="exceed"):
        simulate(series(), plan(fee=1))
    request = plan()
    request["deposits"] = [{"at": 5, "amount": -1}]
    with pytest.raises(ValueError, match="at least 0"):
        simulate(series(), request)
    request = {**plan(), "currency": "EUR"}
    with pytest.raises(ValueError, match="native currency"):
        simulate(series(), request)
    document = series()
    document["actions"] = [dividend(currency="EUR")]
    with pytest.raises(ValueError, match="dividend currency"):
        simulate(document)
    document["actions"][0]["currency"] = "usd"
    with pytest.raises(ValueError, match="uppercase currency"):
        validate_series(document)


def test_futures_and_negative_spot_fills_cannot_use_cashplan():
    with pytest.raises(ValueError, match="futures and references"):
        simulate_cash_plan(series(), plan(), as_of=30, instrument={**INSTRUMENT, "form": "future"})
    with pytest.raises(ValueError, match="positive purchase"):
        simulate(series((-1, 1, 1)))
    with pytest.raises(ValueError, match="positive valuation"):
        simulate(series((100, 1, 0)))
