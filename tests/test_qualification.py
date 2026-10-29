"""Native source qualification uses synthetic records in real SQLite snapshots."""

import hashlib
import json
from dataclasses import replace

import pytest

from crypto_market_analysis.evidence import canonical_json
from crypto_market_analysis.models import Candle, CollectionResult, Observation
from crypto_market_analysis.qualification import NATIVE_MARKETS, qualify_sources
from crypto_market_analysis.storage import Store

NOW = 24 * 3600 + 100


def candle(hour, *, asset="BTC", quote="EUR", source="kraken", mode="live", observed_at=NOW, close=10):
    return Candle(source, asset, quote, "1h", hour * 3600, (hour + 1) * 3600,
                  10, max(10, close), min(10, close), close, 2, observed_at, data_mode=mode)


def request(**overrides):
    return {"sources": ["kraken"], "required_history_days": 1, **overrides}


def one(store, **overrides):
    return qualify_sources(store, request(markets=["BTC/EUR"], **overrides), NOW)["markets"][0]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "synthetic.sqlite")


def test_four_native_markets_qualify_independently_and_remain_offline(store, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Qualification must not load unbounded Python candle history.")

    monkeypatch.setattr(store, "candles", forbidden)
    store.put_candles([candle(h, asset=a, quote=q) for a, q in (m.split("/") for m in NATIVE_MARKETS)
                       for h in range(24)])
    before = store.path.read_bytes()
    result = qualify_sources(store, request(), NOW)
    assert result["status"] == "pass"
    assert [r["market"] for r in result["markets"]] == list(NATIVE_MARKETS)
    assert all(r["status"] == "pass" and r["coverage"]["available_hours"] == 24 for r in result["markets"])
    assert all(r["source_access"]["status"] == "untested" for r in result["markets"])
    assert all(r["capability"]["documented_page_cap"] == 720 for r in result["markets"])
    assert all(r["rights"]["status"] == "intended_use_review_required" for r in result["markets"])
    assert result["resources"]["requests_performed"] == 0
    assert before == store.path.read_bytes()


def test_one_fresh_pair_foreign_quotes_sources_modes_and_context_cannot_hide_missingness(store):
    store.put_candles([candle(h) for h in range(24)])
    store.put_candles([candle(h, asset="ETH", quote="USDT") for h in range(24)])
    store.put_candles([candle(h, asset="ETH", quote="EUR", source="coinbase") for h in range(24)])
    store.put_candles([candle(h, asset="ETH", quote="USD", mode="import") for h in range(24)])
    store.put_observations([Observation("kraken", "ETH", "spot_price", NOW, NOW, 10, "USD", "USD")])
    store.record_run(CollectionResult("kraken", candles=[candle(23)]), "market", NOW)
    result = qualify_sources(store, request(), NOW)
    assert result["status"] == "partial"
    assert [r["status"] for r in result["markets"]] == ["pass", "unavailable", "unavailable", "unavailable"]
    assert result["markets"][1]["coverage"]["missing_hours"] == 24
    assert result["markets"][1]["source_health"]["market_success"] == "unknown"
    assert result["markets"][1]["source_access"]["status"] == "untested"


def test_exact_full_range_gaps_include_leading_internal_and_trailing_hours(store):
    store.put_candles([candle(h) for h in (2, 3, 6, 7, 20)])
    row = one(store, max_gap_details=2)
    coverage = row["coverage"]
    assert row["status"] == "partial"
    assert coverage["required_start"] == 0
    assert coverage["required_end"] == 24 * 3600
    assert coverage["available_hours"] == 5
    assert coverage["missing_hours"] == 19
    assert coverage["gap_count"] == 4
    assert coverage["largest_gap_hours"] == 12
    assert coverage["gap_ranges"] == [{"start": 0, "end": 7200, "hours": 2},
                                       {"start": 14400, "end": 21600, "hours": 2}]
    assert coverage["gap_details_omitted"] == 2
    assert coverage["calculation_truncated"] is False


def test_empty_history_is_one_full_range_gap_without_false_zero_coverage(store):
    row = one(store)
    assert row["status"] == "unavailable"
    assert row["coverage"]["missing_hours"] == 24
    assert row["coverage"]["gap_ranges"] == [{"start": 0, "end": 86400, "hours": 24}]
    assert row["freshness"]["last_success"] is None


def test_known_at_before_ranking_revisions_and_late_arrivals(store):
    store.put_candles([candle(h, observed_at=86400) for h in range(23)])
    store.put_candles([candle(0, close=12, observed_at=NOW + 10), candle(23, observed_at=NOW + 10)])
    before = one(store)
    assert before["coverage"]["available_hours"] == 23
    assert before["coverage"]["revision_rows"] == 0
    assert before["evidence"]["row_samples"][0]["observed_at"] == 86400
    after = qualify_sources(store, request(markets=["BTC/EUR"]), NOW + 10)["markets"][0]
    assert after["status"] == "pass"
    assert after["coverage"]["available_hours"] == 24
    assert after["coverage"]["revision_rows"] == 1
    assert after["evidence"]["row_samples"][0]["observed_at"] == NOW + 10
    assert before["evidence"]["version_rows_sha256"] != after["evidence"]["version_rows_sha256"]


def test_exact_selected_and_version_digests_reference_actual_rows(store):
    store.put_candles([candle(0, observed_at=86400), candle(1, observed_at=86400)])
    store.put_candles([candle(0, close=12)])
    row = one(store, max_evidence_rows=0)
    assert row["evidence"]["samples_truncated"]
    assert row["evidence"]["samples_omitted"] == 2
    with store.connection() as con:
        versions = con.execute("SELECT id,fingerprint,open_time,close_time,observed_at FROM candles "
                               "ORDER BY open_time,observed_at,id").fetchall()
    expected = hashlib.sha256(b"".join((canonical_json(tuple(r)) + "\n").encode() for r in versions)).hexdigest()
    assert row["evidence"]["version_rows_sha256"] == expected
    latest = [versions[1], versions[2]]
    selected = hashlib.sha256(b"".join((canonical_json(tuple(r)) + "\n").encode() for r in latest)).hexdigest()
    assert row["evidence"]["selected_rows_sha256"] == selected


def test_import_receipt_does_not_invent_past_known_at_or_promote_live_coverage(store):
    store.put_candles([candle(h, mode="import", observed_at=NOW + 1) for h in range(24)])
    assert one(store, data_mode="import")["status"] == "unavailable"
    later = qualify_sources(store, request(markets=["BTC/EUR"], data_mode="import"), NOW + 1)
    assert later["status"] == "pass"
    assert later["markets"][0]["coverage"]["earliest_observed_at"] == NOW + 1
    assert "never historical publication time" in later["markets"][0]["coverage"]["historical_known_at"]
    assert one(store)["status"] == "unavailable"


def test_fresh_receipt_of_old_history_is_stale_and_recent_failure_is_separate(store):
    store.put_candles([candle(0)])
    store.record_run(CollectionResult("kraken", errors=["Synthetic provider denial"]), "market", NOW)
    row = one(store, max_gap_hours=23)
    assert row["status"] == "partial"
    assert row["freshness"]["receipt_age_seconds"] == 0
    assert row["freshness"]["close_age_seconds"] > 10800
    assert row["freshness"]["status"] == "stale"
    assert row["source_health"]["last_failure"]["observed_at"] == NOW
    assert row["source_health"]["scope"] == "source_wide_market_and_mode_unknown"
    assert row["source_access"]["status"] == "untested"


def test_sample_budget_is_global_and_does_not_change_aggregate_qualification(store):
    store.put_candles([candle(h, asset=a, quote=q) for a, q in (m.split("/") for m in NATIVE_MARKETS)
                       for h in range(24)])
    result = qualify_sources(store, request(max_evidence_rows=3), NOW)
    assert result["status"] == "pass"
    assert sum(len(r["evidence"]["row_samples"]) for r in result["markets"]) == 3
    assert all(r["coverage"]["available_hours"] == 24 for r in result["markets"])
    assert result["resources"]["evidence_rows_returned"] == 3


def test_gap_detail_budget_is_global_and_does_not_hide_missing_hours(store):
    result = qualify_sources(store, request(max_gap_details=1), NOW)
    assert result["resources"]["gap_details_returned"] == 1
    assert [len(r["coverage"]["gap_ranges"]) for r in result["markets"]] == [1, 0, 0, 0]
    assert all(r["coverage"]["missing_hours"] == 24 for r in result["markets"])
    assert all(r["coverage"]["gap_count"] == 1 for r in result["markets"])
    assert [r["coverage"]["gap_details_omitted"] for r in result["markets"]] == [0, 1, 1, 1]


def test_resource_budgets_never_pass_on_partial_calculation(store):
    store.put_candles([candle(h) for h in range(24)])
    row = one(store, max_query_rows=2)
    assert row["status"] == "unavailable"
    assert row["coverage"]["available_hours"] is None
    assert row["coverage"]["calculation_truncated"] is True
    assert row["reasons"] == ["query_row_budget_exceeded"]
    result = qualify_sources(store, request(max_storage_bytes=1), NOW)
    assert result["status"] == "unavailable"
    assert result["resources"]["storage_budget_exceeded"] is True
    assert result["resources"]["requests_performed"] == 0


def test_time_filter_precedes_row_budget_and_version_selection(store):
    store.put_candles([candle(h) for h in range(24)])
    store.put_candles([candle(h, observed_at=NOW + 2 * 86400) for h in range(24, 48)])
    store.put_candles([candle(0, observed_at=NOW + 1, close=12)])
    row = one(store, max_query_rows=24)
    assert row["status"] == "pass"
    assert row["coverage"]["versions_available"] == 24


def test_history_outside_requested_window_is_not_ranked_or_budgeted(store):
    cutoff = 48 * 3600 + 100
    store.put_candles([candle(h, observed_at=cutoff) for h in range(48)])
    result = qualify_sources(store, request(markets=["BTC/EUR"], required_start=24 * 3600,
                                            max_query_rows=24), cutoff)
    assert result["status"] == "pass"
    assert result["resources"]["candidate_versions_consumed"] == 24
    assert result["resources"]["database_rows"]["candles"] == 48
    assert result["markets"][0]["coverage"]["available_start"] == 24 * 3600


def test_modes_unknown_sources_and_native_market_sources_stay_explicit(store):
    store.put_candles([candle(h, source="synthetic-test-source", mode="demo") for h in range(24)])
    row = qualify_sources(store, request(sources=[], market_sources={"BTC/EUR": ["synthetic-test-source"]},
                                        markets=["BTC/EUR"], data_mode="demo"), NOW)["markets"][0]
    assert row["status"] == "partial"
    assert "synthetic_data_cannot_qualify_a_real_source" in row["reasons"]
    assert row["capability"]["status"] == "unknown"
    assert row["regulatory_eligibility"]["evidence"] == {}


def test_documented_retention_and_entity_do_not_claim_runtime_access(store):
    row = one(store)
    assert "720" in row["capability"]["documented_retention"]
    assert row["source_access"]["status"] == "untested"
    assert row["regulatory_eligibility"]["evidence"]["legal_entity"] == "Payward Europe Solutions Limited"
    assert row["coverage"]["context_lookback_readiness"]["status"] == "not_established_by_hourly_coverage"
    assert row["coverage"]["context_lookback_readiness"]["required_daily_closes_for_requested_day_return"] == 2


def test_only_scoped_public_capture_can_establish_runtime_success(store):
    with store.connection() as con:
        con.execute("CREATE TABLE history_captures(id TEXT PRIMARY KEY,asset TEXT,quote TEXT,materialized_at INTEGER,payload TEXT)")
        for index, (quote, mode, kind) in enumerate((("USD", "import", "public_rest_adapter"),
                                                   ("EUR", "import", "injected_client_unqualified"),
                                                   ("EUR", "import", "public_rest_adapter"))):
            payload = {"source": "kraken", "data_mode": mode, "captured_at": NOW,
                       "capture_evidence": kind, "acquisition": "public_rest", "raw_response_sha256": "synthetic"}
            con.execute("INSERT INTO history_captures VALUES(?,?,?,?,?)",
                        (str(index), "BTC", quote, NOW, json.dumps(payload)))
    assert one(store)["source_access"]["status"] == "untested"
    imported = one(store, data_mode="import")
    assert imported["source_access"]["status"] == "success"
    assert imported["source_access"]["evidence"]["id"] == "2"
    assert imported["status"] == "unavailable"
    assert qualify_sources(store, request(markets=["ETH/EUR"], data_mode="import"), NOW)["markets"][0]["source_access"]["status"] == "untested"


@pytest.mark.parametrize("overrides", [
    {"markets": ["BTC/USDT"]}, {"markets": ["BTC/EUR", "BTC/EUR"]}, {"sources": []},
    {"sources": "kraken"}, {"sources": ["kraken", "kraken"]}, {"data_mode": "mixed"},
    {"required_history_days": True}, {"required_history_days": 0}, {"max_evidence_rows": -1},
    {"required_start": 1}, {"required_end": NOW + 3600}, {"max_query_rows": 0},
    {"market_sources": {"ETH/USDT": ["kraken"]}},
])
def test_request_validation_rejects_ambiguous_or_unbounded_inputs(store, overrides):
    with pytest.raises(ValueError):
        qualify_sources(store, request(**overrides), NOW)


def test_revision_evidence_is_stable_but_changes_when_selected_data_changes(store):
    store.put_candles([candle(0)])
    first = one(store)
    assert first == one(store)
    store.put_candles([replace(candle(0), close=11, high=11)])
    revised = one(store)
    assert first["evidence"]["reference"] != revised["evidence"]["reference"]
    assert revised["coverage"]["revision_rows"] == 1
