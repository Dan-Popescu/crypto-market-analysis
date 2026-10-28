"""Hand-authored quote evidence; no public feeds or implicit fiat conversions."""

import copy
from decimal import Decimal

import pytest

from crypto_market_analysis.quote_context import analyze_quote_context


def observation(base="EUR", quote="USD", value="1.1", **overrides):
    return {
        "base": base,
        "quote": quote,
        "value": value,
        "event_time": 1000,
        "observed_at": 1001,
        "source": "synthetic-source",
        "source_url": "https://example.invalid/synthetic-quote",
        "data_version": "synthetic-v1",
        "data_mode": "demo",
        "price_type": "indicative_reference" if base in {"EUR", "USD"} else "observed_spot",
        **overrides,
    }


def test_reference_orientation_and_decimal_basis_preserve_input():
    request = {
        "data_mode": "demo",
        "fx_reference": observation("USD", "EUR", "0.8"),
        "stablecoin_prices": [observation("USDT", "EUR", "0.792"), observation("USDC", "USD", "1.001")],
    }
    original = copy.deepcopy(request)
    result = analyze_quote_context(request, 1100)
    assert request == original
    fx = result["fx_reference"]
    assert fx["eurusd_reference"] == "1.25"
    assert fx["transformation"]["operation"] == "reciprocal"
    assert not fx["executable"]
    eur, usd = result["stablecoin_basis"]
    assert eur["basis_percent"] == "-1.00000"
    assert eur["usd_per_token"] == "0.99000"
    assert eur["native_quote"] == eur["observation"]["quote"] == "EUR"
    assert eur["transformation"]["operation"] == "explicit_fx_conversion"
    assert eur["transformation"]["fx_data_version"] == "synthetic-v1"
    assert usd["basis_percent"] == "0.100"
    assert usd["transformation"]["operation"] == "identity"
    result["stablecoin_basis"][0]["observation"]["source"] = "changed copy"
    assert request == original


def test_direct_usd_needs_no_fx_and_eur_requires_explicit_reference():
    result = analyze_quote_context({
        "data_mode": "demo",
        "stablecoin_prices": [observation("USDT", "USD", "0.99"), observation("USDT", "EUR", "0.9")],
    }, 1100)
    assert result["fx_reference"]["status"] == "missing"
    usd, eur = result["stablecoin_basis"]
    assert usd["basis_percent"] == "-1.00"
    assert eur["status"] == "fx_reference_missing" and eur["basis_percent"] is None


@pytest.mark.parametrize(
    ("fx_changes", "price_changes", "as_of", "max_age", "status"),
    [
        ({"observed_at": 1300}, {}, 1200, 86400, "fx_reference_not_available_as_of"),
        ({}, {"observed_at": 1300}, 1200, 86400, "not_available_as_of"),
        ({"event_time": 100, "observed_at": 1100}, {}, 1200, 300, "fx_reference_stale"),
        ({}, {"event_time": 100, "observed_at": 1100}, 1200, 300, "stale"),
        ({"event_time": 600}, {}, 1200, 86400, "endpoint_skew_exceeded"),
    ],
)
def test_stale_late_and_skew_never_manufacture_a_common_endpoint(
    fx_changes, price_changes, as_of, max_age, status
):
    result = analyze_quote_context({
        "data_mode": "demo", "max_age_seconds": max_age,
        "fx_reference": observation(**fx_changes),
        "stablecoin_prices": [observation("USDT", "EUR", "0.9", **price_changes)],
    }, as_of)
    item = result["stablecoin_basis"][0]
    assert item["status"] == status
    assert item["basis_percent"] is None and item["usd_per_token"] is None
    assert item["observation"]["quote"] == "EUR"


def test_endpoint_and_freshness_boundaries_are_inclusive():
    result = analyze_quote_context({
        "data_mode": "demo", "max_age_seconds": 400, "max_endpoint_skew_seconds": 300,
        "fx_reference": observation(event_time=700),
        "stablecoin_prices": [observation("USDT", "EUR", "0.9")],
    }, 1100)
    assert result["stablecoin_basis"][0]["status"] == "available"
    assert result["stablecoin_basis"][0]["endpoint_skew_seconds"] == 300


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", "1e100000", "1e-1000", True, 1.1, "x"])
def test_invalid_decimal_prices_are_rejected(value):
    with pytest.raises(ValueError):
        analyze_quote_context({"data_mode": "demo", "fx_reference": observation(value=value)}, 1100)


@pytest.mark.parametrize("changes", [
    {"data_mode": "live"}, {"observed_at": 999}, {"source_url": "http://example.invalid"},
    {"price_type": "executable"}, {"base": "USDT"}, {"observed_at": True},
])
def test_invalid_or_mixed_reference_semantics_are_rejected(changes):
    with pytest.raises(ValueError):
        analyze_quote_context({"data_mode": "demo", "fx_reference": observation(**changes)}, 1100)


def test_limits_empty_missing_and_unknown_fields():
    assert analyze_quote_context({"data_mode": "import"}, 0)["stablecoin_basis"] == []
    for request in (
        {"data_mode": "demo", "stablecoin_prices": [observation("USDT")] * 101},
        {"data_mode": "demo", "max_age_seconds": True},
        {"data_mode": "demo", "max_endpoint_skew_seconds": -1},
        {"data_mode": "demo", "implicit_conversion": True},
        {"data_mode": "demo", "fx_reference": observation(source="x" * (1024 * 1024))},
    ):
        with pytest.raises(ValueError):
            analyze_quote_context(request, 1100)


def test_shareable_synthetic_request_example_is_usable_at_its_declared_endpoint():
    import json
    from pathlib import Path

    request = json.loads((Path(__file__).parents[1] / "examples" / "quote-context.json").read_text())
    result = analyze_quote_context(request, request["fx_reference"]["observed_at"])
    eur, usd = result["stablecoin_basis"]
    assert Decimal(eur["basis_percent"]) == Decimal("-1")
    assert Decimal(usd["basis_percent"]) == Decimal("0.1")
