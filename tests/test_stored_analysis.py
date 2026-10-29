"""SQLite-backed bridges preserve identity, historical vintages and unknowns."""

from contextlib import contextmanager

import pytest

from crypto_market_analysis.evidence import content_hash
from crypto_market_analysis.models import Observation
from crypto_market_analysis.storage import Store
from crypto_market_analysis.stored_analysis import (
    analyze_stored_derivatives,
    analyze_stored_quotes,
    analyze_stored_venues,
    build_stored_derivatives_inputs,
)

NOW = 172800


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "stored.sqlite")


def oi(event_time, value, **overrides):
    values = {"source": "venue", "asset": "BTC", "metric": "open_interest_amount",
              "event_time": event_time, "observed_at": event_time, "value": value,
              "unit": "BTC", "quote": "USDT", "data_mode": "import", "metadata": {
                  "symbol": "BTC/USDT:USDT", "settle": "USDT", "market_type": "swap",
                  "counting_convention": "one_sided"}}
    return Observation(**{**values, **overrides})


def derivatives_request(**overrides):
    return {"asset": "BTC", "source": "venue", "quote": "USDT", "data_mode": "import", **overrides}


def quote_row(**overrides):
    return {"record_id": "ecb:EUR/USD:2026-10-28", "source": "ecb", "base": "EUR", "quote": "USD",
            "value": "1.10", "event_time": None, "observed_at": NOW,
            "materialized_at": NOW, "source_url": "https://example.org/official-rate",
            "data_version": "sha256:fixture", "price_type": "indicative_reference", "data_mode": "import",
            "timestamp_status": "date_only", "source_date": "2026-10-28", **overrides}


def test_history_preserves_old_24h_endpoint_and_late_revision(store):
    start = NOW - 86400
    four = NOW - 14400
    rows = [oi(start, 90), oi(four, 100), oi(NOW, 120),
            oi(four, 999, observed_at=four + 1), oi(NOW, 777, observed_at=NOW + 1)]
    # More than a generic latest-256 capture can retain, within one native series.
    rows.extend(oi(NOW - offset, 110) for offset in range(1, 401))
    store.put_observations(rows)
    result = analyze_stored_derivatives(store, derivatives_request(), NOW)
    comparisons = result["series"][0]["comparisons"]
    assert comparisons["24h"]["native_change"] == 30
    assert comparisons["4h"]["native_change"] == 20
    assert comparisons["4h"]["start"]["value"] == 100
    assert len(result["stored_inputs"]["consumed_records"]) == 404
    assert result["stored_inputs"]["truncation"]["truncated"] is False
    built = build_stored_derivatives_inputs(store, derivatives_request(), NOW)
    evidence = built["stored_inputs"]
    assert evidence["input_hash"] == content_hash({"request": built["request"], "records": evidence["consumed_records"]})


def test_source_quote_mode_are_filtered_before_required_budget(store):
    store.put_observations([oi(NOW, 120), oi(NOW, 999, source="other"),
                            oi(NOW, 999, quote="USD"), oi(NOW, 999, data_mode="live")])
    snapshot = store.stored_analysis_snapshot("derivatives", as_of=NOW, mode="import", sources=["venue"],
                                              asset="BTC", quote="USDT", limit=1)
    assert [row["payload"]["value"] for row in snapshot["records"]] == [120]
    assert analyze_stored_derivatives(store, derivatives_request(data_mode="demo"), NOW)["series"] == []
    with pytest.raises(ValueError, match="explicit data_mode"):
        analyze_stored_derivatives(store, {"asset": "BTC", "source": "venue", "quote": "USDT"}, NOW)


def test_required_truncation_fails_instead_of_dropping_endpoint(store):
    store.put_observations([oi(NOW - 86400, 90), oi(NOW - 14400, 100), oi(NOW, 120)])
    with pytest.raises(ValueError, match="exceeds the 2-row budget"):
        store.stored_analysis_snapshot("derivatives", as_of=NOW, mode="import", sources=["venue"],
                                       asset="BTC", quote="USDT", limit=2)


def test_snapshot_watermark_and_rows_exclude_interleaved_writer(store, monkeypatch):
    store.put_observations([oi(NOW - 1, 100)])
    original = store.connection
    writer = Store(store.path)
    inserted = False

    class InterleavedConnection:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, parameters=()):
            nonlocal inserted
            result = self.con.execute(sql, parameters)
            if "MAX(id)" in sql and "observations" in sql and not inserted:
                # SELECT has established the reader's snapshot before this write.
                inserted = True
                writer.put_observations([oi(NOW, 200)])
            return result

    @contextmanager
    def interleaved():
        with original() as con:
            yield InterleavedConnection(con)

    monkeypatch.setattr(store, "connection", interleaved)
    result = analyze_stored_derivatives(store, derivatives_request(), NOW)
    assert result["series"][0]["current"]["value"] == 100
    assert result["stored_inputs"]["watermark"]["observations"] == 1
    assert writer.watermark()["observations"] == 2


def test_unknown_conventions_are_preserved_without_native_change(store):
    store.put_observations([oi(NOW, 120, metadata={"symbol": "BTC/USDT:USDT", "settle": "USDT"})])
    result = analyze_stored_derivatives(store, derivatives_request(), NOW)
    assert result["series"][0]["status"] == "unknown_native_convention"
    assert result["series"][0]["comparisons"]["24h"]["native_change"] is None
    assert "counting_convention" not in result["stored_inputs"]["consumed_records"][0]["payload"]["metadata"]


def test_date_only_fx_preserves_rate_date_and_unknown_endpoint(store):
    store.save_local_record("fx_reference", quote_row())
    store.save_local_record("stablecoin_price", quote_row(
        record_id="venue:USDT/EUR", source="venue", base="USDT", quote="EUR", value="0.91",
        event_time=NOW, price_type="observed_spot", timestamp_status="exchange"))
    result = analyze_stored_quotes(store, {"sources": ["ecb", "venue"], "quote": "EUR", "data_mode": "import"}, NOW)
    assert result["fx_reference"]["status"] == "unknown_timestamp"
    assert result["fx_reference"]["observation"]["source_date"] == "2026-10-28"
    assert result["fx_reference"]["observation"]["value"] == "1.10"
    assert result["fx_reference"]["observation"]["event_time"] is None
    assert result["stablecoin_basis"][0]["status"] == "fx_reference_unknown_timestamp"
    assert result["stablecoin_basis"][0]["basis_percent"] is None
    assert len(result["stored_inputs"]["input_references"]) == 2


def test_latest_known_at_quote_version_and_native_mode_selection(store):
    original = quote_row(event_time=NOW - 1, observed_at=NOW - 1)
    store.save_local_record("fx_reference", original)
    store.save_local_record("fx_reference", {**original, "value": "1.20", "observed_at": NOW + 1})
    store.save_local_record("stablecoin_price", quote_row(
        record_id="venue:USDT/USD", source="venue", base="USDT", value="1.01",
        event_time=NOW, price_type="observed_spot"))
    store.save_local_record("stablecoin_price", quote_row(
        record_id="venue:USDT/EUR", source="venue", base="USDT", quote="EUR", value="0.9",
        event_time=NOW, price_type="observed_spot"))
    request = {"sources": ["ecb", "venue"], "quote": "USD", "data_mode": "import"}
    result = analyze_stored_quotes(store, request, NOW)
    assert result["fx_reference"]["eurusd_reference"] == "1.10"
    assert len(result["stablecoin_basis"]) == 1
    assert result["stablecoin_basis"][0]["basis_percent"] == "1.00"
    assert analyze_stored_quotes(store, {**request, "data_mode": "live"}, NOW)["fx_reference"]["status"] == "missing"


def test_missing_quote_clock_remains_unknown_and_multiple_fx_sources_are_ambiguous(store):
    store.save_local_record("stablecoin_price", quote_row(
        record_id="venue:USDT/USD", source="venue", base="USDT", value="1", price_type="observed_spot"))
    result = analyze_stored_quotes(store, {"sources": ["venue"], "quote": "USD", "data_mode": "import"}, NOW)
    assert result["stablecoin_basis"][0]["status"] == "unknown_timestamp"
    assert result["stablecoin_basis"][0]["basis_percent"] is None
    store.save_local_record("fx_reference", quote_row())
    store.save_local_record("fx_reference", quote_row(record_id="other:EUR/USD", source="other"))
    with pytest.raises(ValueError, match="never pooled"):
        analyze_stored_quotes(store, {"sources": ["ecb", "other"], "quote": "USD", "data_mode": "import"}, NOW)


def test_empty_reads_perform_no_collection_or_journal_writes(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only analysis attempted an external call or journal write")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(store, "save_local_record", forbidden)
    monkeypatch.setattr(store, "record_run", forbidden)
    before = store.counts()
    assert analyze_stored_derivatives(store, derivatives_request(), NOW)["series"] == []
    result = analyze_stored_quotes(store, {"sources": ["ecb"], "quote": "EUR", "data_mode": "import"}, NOW)
    assert result["fx_reference"]["status"] == "missing"
    assert result["stored_inputs"]["consumed_records"] == []
    assert store.counts() == before


def test_venue_bridge_consumes_exact_native_books_and_retains_unknown_clock(store):
    from crypto_market_analysis.book_snapshots import retain_books, retained_book

    def book(source, *, quote="EUR", mode="import", timestamp=NOW * 1000):
        return retained_book({"timestamp": timestamp, "bids": [["99", "2"]], "asks": [["101", "2"]]},
                             source=source, asset="BTC", quote=quote, symbol=f"BTC/{quote}",
                             captured_at=NOW, requested_limit=500, data_mode=mode)

    known, unknown = book("venue"), book("unknown", timestamp=None)
    retain_books(store, [known, unknown, book("other", quote="USD"), book("other", mode="live")])
    request = {"analysis": "disagreement", "asset": "BTC", "quote": "EUR", "data_mode": "import",
               "sources": ["venue", "unknown", "other"]}
    result = analyze_stored_venues(store, request, NOW)
    assert result["coverage"]["present_sources"] == ["venue"]
    assert result["coverage"]["missing_sources"] == ["unknown", "other"]
    evidence = result["stored_inputs"]
    assert evidence["input_references"] == [known["input_reference"], unknown["input_reference"]]
    assert evidence["unknown_inputs"] == [{"source": "unknown", "reason": "unknown_exchange_event_time"}]
    assert evidence["watermark"]["max_eligible_id"] == 2
    assert evidence["truncation"]["selection_truncated"] is False
    assert len(evidence["consumed_records"]) == 1


def test_purchase_bridge_keeps_missing_cost_and_product_assumptions_unknown(store):
    from crypto_market_analysis.book_snapshots import retain_books, retained_book

    book = retained_book({"timestamp": NOW * 1000, "bids": [["99", "2"]], "asks": [["101", "2"]]},
                         source="venue", asset="BTC", quote="EUR", symbol="BTC/EUR",
                         captured_at=NOW, requested_limit=500, data_mode="import")
    retain_books(store, [book])
    request = {"analysis": "purchase_costs", "asset": "BTC", "quote": "EUR", "data_mode": "import",
               "sources": ["venue"], "budget": "100"}
    result = analyze_stored_venues(store, request, NOW)
    assert result["rows"][0]["hypothetical_fill"] is None
    assert result["rows"][0]["assumptions"] is None
    assert result["rows"][0]["product"] is None
    assert result["rows"][0]["purchase_eligibility"]["status"] == "unknown"
    assert "unknown_cost_and_product_rules" in result["rows"][0]["reasons"]
    assert result["stored_inputs"]["consumed_records"][0]["asks"] == [{"price": "101", "quantity": "2"}]


def test_empty_venue_bridge_is_read_only_and_requires_matching_fields(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only analysis attempted an external call or journal write")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(store, "record_run", forbidden)
    request = {"analysis": "disagreement", "asset": "BTC", "quote": "EUR", "data_mode": "import",
               "sources": ["venue"]}
    before = store.counts()
    result = analyze_stored_venues(store, request, NOW)
    assert result["rows"][0]["status"] == "missing"
    assert result["stored_inputs"]["consumed_records"] == []
    assert store.counts() == before
    with store.connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='book_snapshots'").fetchone() is None
    with pytest.raises(ValueError, match="fields must match"):
        analyze_stored_venues(store, {**request, "budget": "100"}, NOW)
