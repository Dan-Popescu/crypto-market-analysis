"""Catalogue qualification and context unit/time semantics."""

from dataclasses import replace

from crypto_market_analysis.models import Observation
from crypto_market_analysis.source_catalog import (
    FRANCE_PURCHASE_ENTITIES,
    SOURCE_PROFILES,
    etf_flow_row,
    interpret_context,
    source_catalog,
)


def observation(value, time, *, unit="fraction", metadata=None, metric="funding_rate", **kwargs):
    return Observation(
        "synthetic_venue",
        "BTC",
        metric,
        time,
        time + 1,
        value,
        unit,
        "USDT",
        metadata or {"funding_interval_seconds": 28800, "value_kind": "actual"},
        **kwargs,
    )


def test_catalog_separates_documentation_access_rights_and_french_entity_scope():
    catalog = source_catalog()
    venues = [profile for profile in SOURCE_PROFILES.values() if profile.role == "global_market_analysis"]
    assert len(venues) == 15
    assert {"binance", "bitget", "kucoin", "bitvavo"} <= {profile.id for profile in venues}
    assert set(FRANCE_PURCHASE_ENTITIES) == {"kraken", "coinbase", "bitvavo"}
    assert FRANCE_PURCHASE_ENTITIES["kraken"]["legal_entity"] == "Payward Europe Solutions Limited"
    assert FRANCE_PURCHASE_ENTITIES["coinbase"]["legal_entity"] == "Coinbase Luxembourg S.A."
    assert FRANCE_PURCHASE_ENTITIES["bitvavo"]["services"] == [
        "custody_and_administration",
        "operating_crypto_trading_platform",
        "transfer_services",
    ]
    assert "portfolio_management" not in FRANCE_PURCHASE_ENTITIES["coinbase"]["services"]
    for source in catalog["sources"]:
        for endpoint in source["endpoints"]:
            assert endpoint["access_status"] == "untested" and endpoint["access_checked_at"] is None
            assert endpoint["documentation_checked_at"] == "2026-10-28"
            assert endpoint["rights_url"].startswith("https://")
            assert endpoint["earliest_verified_time"] is None
    catalog["sources"][0]["name"] = "mutated copy"
    assert source_catalog()["sources"][0]["name"] != "mutated copy"


def test_endpoint_retention_and_free_key_are_explicit():
    assert SOURCE_PROFILES["kraken"].endpoints[0].page_cap == 720
    assert "720" in SOURCE_PROFILES["kraken"].endpoints[0].retention
    assert SOURCE_PROFILES["bitvavo"].endpoints[0].page_cap == 1440
    assert SOURCE_PROFILES["bitvavo"].endpoints[1].timestamp_unit == "nanoseconds"
    assert SOURCE_PROFILES["fred_api"].endpoints[0].authentication == "free_registered_key"
    assert SOURCE_PROFILES["fred_csv"].endpoints[0].authentication == "none"
    assert SOURCE_PROFILES["farside"].endpoints[0].implementation == "manual_import_fallback"


def test_funding_changes_never_mix_period_or_projected_actual_values():
    observations = [
        observation(0.001, 10),
        observation(0.003, 20),
        observation(0.02, 30, metadata={"funding_interval_seconds": 3600, "value_kind": "actual"}),
        observation(0.04, 40, metadata={"funding_interval_seconds": 28800, "value_kind": "projected"}),
    ]
    result = interpret_context(observations, 50)
    assert len(result["observations"]) == 3
    actual = next(row for row in result["observations"] if row["event_time"] == 20)
    assert actual["change"] == 0.002
    assert actual["change_unit"] == "fraction" and actual["change_period_seconds"] == 10
    assert all(row["change"] is None for row in result["observations"] if row["event_time"] != 20)


def test_unknown_funding_period_and_oi_unit_stay_noncomparable():
    rows = [
        observation(1, 10, unit="unknown", metric="open_interest", metadata={"instrument": "BTC-contract"}),
        observation(2, 20, unit="unknown", metric="open_interest", metadata={"instrument": "BTC-contract"}),
        observation(0.01, 30, metadata={"value_kind": "actual"}),
    ]
    result = interpret_context(rows, 50)
    assert all(not row["comparable"] and row["change"] is None for row in result["observations"])
    assert "positioning" in result["observations"][1]["limitations"][-1] or any(
        "positioning" in limitation for row in result["observations"] for limitation in row["limitations"]
    )


def test_revisions_future_capture_and_context_age_are_preserved():
    first = observation(1, 10)
    revised = replace(first, value=2, observed_at=30)
    future_capture = replace(first, value=100, observed_at=100)
    result = interpret_context([first, revised, future_capture], 50)
    assert result["excluded_future_observations"] == 1
    [row] = result["observations"]
    assert row["value"] == 2 and row["observed_at"] == 30
    assert row["event_age_seconds"] == 40 and row["ingestion_age_seconds"] == 20


def test_etf_dashed_session_is_pending_even_when_total_is_zero():
    kwargs = dict(
        asset="BTC",
        session_date="2026-10-28",
        observed_at=100,
        source_url="https://example.test/synthetic-table",
    )
    row = etf_flow_row(**kwargs, fund_values=["-", "—", None], total="0.0")
    assert row["status"] == "pending" and row["value"] is None and row["reported_total"] == 0
    row = etf_flow_row(**kwargs, fund_values=["3", "-", "-1"], total="2")
    assert row["status"] == "partial" and row["value"] is None
    row = etf_flow_row(**kwargs, fund_values=["3", "-3"], total="0.0")
    assert row["status"] == "reported" and row["value"] == 0 and row["unit"] == "USD_millions"


def test_pending_observation_does_not_become_measured_zero_or_change():
    rows = [
        observation(3, 10, unit="USD_millions", metric="etf_net_flow", metadata={"status": "reported"}),
        observation(0, 20, unit="USD_millions", metric="etf_net_flow", metadata={"status": "pending"}),
    ]
    [row] = interpret_context(rows, 50)["observations"]
    assert row["status"] == "pending" and row["change"] is None and not row["comparable"]


def test_funding_interval_aliases_require_explicit_units_and_agree():
    rows = [
        observation(1, 10, metadata={"value_kind": "actual", "funding_interval_hours": 8}),
        observation(2, 20, metadata={"value_kind": "actual", "funding_interval": "8h"}),
        observation(3, 30, metadata={"value_kind": "actual", "funding_interval": "8"}),
        observation(
            4,
            40,
            metadata={"value_kind": "actual", "funding_interval_hours": 8, "funding_interval_seconds": 3600},
        ),
    ]
    result = interpret_context(rows, 50)
    known = next(row for row in result["observations"] if row["event_time"] == 20)
    assert known["funding_interval_seconds"] == 28800 and known["change"] == 1
    unknown = next(row for row in result["observations"] if row["event_time"] == 40)
    assert unknown["funding_interval_seconds"] is None and unknown["change"] is None


def test_etf_fund_values_without_source_total_remain_partial():
    row = etf_flow_row(
        asset="ETH",
        session_date="2026-10-28",
        fund_values=[1, 2],
        total="-",
        observed_at=100,
        source_url="https://example.test/synthetic-table",
    )
    assert row["status"] == "partial" and row["value"] is None and row["reported_fund_count"] == 2
