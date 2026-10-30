"""Two-cutoff changes preserve native identities, availability and read isolation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from crypto_market_analysis.analysis_changes import build_analysis_changes, render_analysis_changes_markdown
from crypto_market_analysis.book_snapshots import retain_books, retained_book
from crypto_market_analysis.models import Candle, CollectionResult, Observation
from crypto_market_analysis.storage import Store

PREVIOUS = 4 * 86400
NOW = PREVIOUS + 3600


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "observer.sqlite")


def candle(event=PREVIOUS, price=100, **changes):
    return Candle("venue", "BTC", "EUR", "1h", event - 3600, event,
                  price, price, price, price, 1, event, data_mode="import", **changes)


def request(**changes):
    return {"dossier": {"data_mode": "import", "markets": [
        {"asset": "BTC", "source": "venue", "quote": "EUR", "lookbacks": [1]}]},
        "previous_as_of": PREVIOUS, **changes}


def change(result, suffix):
    return next(row for row in result["changes"] if row["path"].endswith(suffix))


def test_late_history_does_not_crowd_analytical_changes_out_of_retained_slots(store):
    store.put_candles([replace(candle(event=hour + 3600), observed_at=NOW)
                       for hour in range(PREVIOUS - 72 * 3600, PREVIOUS, 3600)])
    result = build_analysis_changes(store, request(max_changes=1), NOW)
    assert result["summary"]["by_category"]["late_arrival"] >= 72
    assert result["omissions"]["truncated"] and len(result["changes"]) == 1
    assert "/context_metrics/" in result["changes"][0]["path"]
    assert result["changes"][0]["market_movement"] is False


def test_nested_sessions_reuse_connection_and_expire(store, monkeypatch):
    with store.read_session() as session:
        with session.read_session() as nested:
            assert nested is session
            with nested.connection() as first, session.connection() as second:
                assert first is second
        # Leaving an inner scope does not expire its owning scope.
        with session.connection() as con:
            assert con.in_transaction
        monkeypatch.setattr("crypto_market_analysis.storage.time.monotonic", lambda: session._started + 16)
        with pytest.raises(ValueError, match="lifetime"):
            with session.read_session():
                pass
        monkeypatch.undo()
    with pytest.raises(ValueError, match="expired"):
        with session.read_session():
            pass


def test_both_dossiers_share_watermark_despite_concurrent_correction(store, monkeypatch):
    store.put_candles([candle()])
    from crypto_market_analysis.analysis_dossier import capture_analysis_dossier
    original = capture_analysis_dossier
    called = 0

    def between(session, selection, cutoff, **kwargs):
        nonlocal called
        result = original(session, selection, cutoff, **kwargs)
        called += 1
        if called == 1:
            def writer():
                Store(store.path).put_candles([replace(candle(), open=900, high=900, low=900, close=900,
                                                       observed_at=NOW)])
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(writer).result(timeout=5)
        return result

    monkeypatch.setattr("crypto_market_analysis.analysis_changes.capture_analysis_dossier", between)
    result = build_analysis_changes(store, request(), NOW)
    assert result["watermark"]["candles"] == 1
    assert result["summary"]["market_movements"] == 0
    assert not any(row["category"] == "same_event_correction" for row in result["changes"])
    monkeypatch.undo()
    later = build_analysis_changes(store, request(), NOW)
    assert change(later, "/price_anchor")["category"] == "same_event_correction"
    assert change(later, "/price_anchor")["before"]["value"] == 100
    assert change(later, "/price_anchor")["after"]["value"] == 900
    assert later["summary"]["market_movements"] == 0


def test_new_native_prices_are_observations_and_missing_does_not_become_zero(store):
    store.put_candles([candle(), candle(NOW, 110)])
    result = build_analysis_changes(store, request(), NOW)
    anchor = change(result, "/price_anchor")
    assert anchor["category"] == "new_event_observation"
    assert anchor["market_movement"]
    assert anchor["after"]["unit"] == "EUR per BTC"
    assert result["summary"]["new_hourly_observations"] == 1
    empty = request()
    empty["dossier"]["markets"][0]["asset"] = "ETH"
    result = build_analysis_changes(store, empty, NOW)
    assert result["summary"]["market_movements"] == 0
    assert result["changes"] == []
    assert "No trading recommendation" in render_analysis_changes_markdown(result)


def test_time_only_and_receipt_only_cannot_be_market_moves(store):
    store.put_candles([candle()])
    result = build_analysis_changes(store, request(), PREVIOUS + 7200)
    assert result["summary"]["market_movements"] == 0
    assert change(result, "/freshness")["category"] == "clock_or_expiry_change"
    assert change(result, "/freshness")["after"]["status"] == "stale"
    store.put_candles([replace(candle(), observed_at=NOW, volume=1.0)])
    result = build_analysis_changes(store, request(), NOW)
    assert change(result, "/price_anchor")["category"] == "receipt_only_update"
    assert result["summary"]["market_movements"] == 0


def test_late_arrivals_and_non_anchor_corrections_are_separate(store):
    store.put_candles([candle(), replace(candle(PREVIOUS - 3600, 95), observed_at=NOW)])
    result = build_analysis_changes(store, request(), NOW)
    late = change(result, f"/candle/{PREVIOUS - 7200}")
    assert late["category"] == "late_arrival"
    assert late["before"] is None
    assert late["after"]["value"]["close"] == 95
    store.put_candles([candle(PREVIOUS - 7200, 90), replace(candle(PREVIOUS - 7200, 92), observed_at=NOW)])
    result = build_analysis_changes(store, request(), NOW)
    revised = change(result, f"/candle/{PREVIOUS - 10800}")
    assert revised["category"] == "same_event_correction"
    assert not revised["market_movement"]


def test_all_four_native_markets_remain_separate(store):
    wanted = request()
    wanted["dossier"]["markets"] = []
    for asset in ("BTC", "ETH"):
        for quote in ("EUR", "USD"):
            wanted["dossier"]["markets"].append({"asset": asset, "source": "venue", "quote": quote, "lookbacks": [1]})
            store.put_candles([replace(candle(), asset=asset, quote=quote),
                               replace(candle(NOW, 110 if asset == "BTC" and quote == "EUR" else 100),
                                       asset=asset, quote=quote)])
    result = build_analysis_changes(store, wanted, NOW)
    moves = [row for row in result["changes"] if row["market_movement"]]
    assert len(moves) == 1
    assert moves[0]["after"]["identity"]["asset"] == "BTC"
    assert moves[0]["after"]["identity"]["quote"] == "EUR"


def test_two_dense_four_market_snapshots_fit_the_shared_sqlite_budget(store):
    cutoff = 800 * 86400
    markets = [{"asset": asset, "source": "venue", "quote": quote, "lookbacks": [7, 30, 365, 730]}
               for asset in ("BTC", "ETH") for quote in ("EUR", "USD")]
    for market in markets:
        store.put_candles([replace(candle(event=event), asset=market["asset"], quote=market["quote"])
                           for event in range(3600, cutoff + 1, 3600)])
    # Both cutoffs need all 76800 bars: this exercises the actual shared read
    # session rather than independently resetting its VM budget per snapshot.
    result = build_analysis_changes(store, {"dossier": {"data_mode": "import", "markets": markets},
                                            "previous_as_of": cutoff}, cutoff + 1)
    assert result["resource_limits"]["consumed_candles"] == 153600
    assert result["resource_limits"]["sqlite_vm_steps"] == 50_000_000
    assert result["watermark"]["candles"] == 76800
    assert result["read_only"] is True
    assert result["summary"]["total_changes"] == 0
    assert result["changes"] == []
    assert len(result["markets"]) == 4
    assert all(market["before"] == market["after"] for market in result["markets"])


def test_calendar_cancellation_and_revision_use_known_at_and_sequence(store):
    wanted = request()
    wanted["dossier"]["calendar"] = {"source": "calendar", "start": PREVIOUS, "end": NOW + 86400}
    row = {"source": "calendar", "event_id": "rate", "event_time": NOW + 100,
           "observed_at": PREVIOUS, "data_mode": "import", "status": "planned"}
    store.save_local_record("calendar", row)
    store.save_local_record("calendar", {**row, "observed_at": NOW, "status": "cancelled"})
    result = build_analysis_changes(store, wanted, NOW)
    revised = next(row for row in result["changes"] if row["path"].startswith("calendar/"))
    assert revised["category"] == "calendar_revision"
    assert revised["before"]["value"]["status"] == "planned"
    assert revised["after"]["value"]["status"] == "cancelled"
    assert revised["after"]["known_at"] == NOW
    store.save_local_record("calendar", {**row, "event_id": "moved", "event_time": NOW + 100})
    store.save_local_record("calendar", {**row, "event_id": "moved", "observed_at": NOW, "event_time": NOW + 10 * 86400})
    result = build_analysis_changes(store, wanted, NOW)
    assert any(row["after"] and row["after"]["value"].get("event_time") == NOW + 10 * 86400
               for row in result["changes"] if row["path"].startswith("calendar/"))


def test_derivatives_identity_and_native_quotes_and_receipt_only_numbers(store):
    wanted = request()
    market = wanted["dossier"]["markets"][0]
    market["derivatives"] = {"source": "derivative", "quote": "USDT"}
    market["quotes"] = {"sources": ["quote-source"]}
    row = Observation("derivative", "BTC", "open_interest_amount", PREVIOUS, PREVIOUS, 100, "BTC", "USDT",
                      {"symbol": "BTC/USDT:USDT", "settle": "USDT", "counting_convention": "one_sided"}, "import")
    store.put_observations([row, replace(row, value=150, event_time=NOW, observed_at=NOW),
                            replace(row, value=999, event_time=NOW, observed_at=NOW, unit="ETH")])
    quoted = {"record_id": "usdt", "source": "quote-source", "base": "USDT", "quote": "EUR",
              "value": "1.0", "event_time": PREVIOUS, "observed_at": PREVIOUS, "data_mode": "import",
              "source_url": "https://example.test/quote", "data_version": "synthetic-v1", "price_type": "observed_spot"}
    store.save_local_record("stablecoin_price", quoted)
    store.save_local_record("stablecoin_price", {**quoted, "value": "1.00", "observed_at": NOW})
    result = build_analysis_changes(store, wanted, NOW)
    derivative = [row for row in result["changes"] if "/derivatives/" in row["path"]]
    assert len(derivative) == 2
    btc = next(row for row in derivative if row["after"]["unit"] == "BTC")
    assert btc["before"]["value"] == 100
    assert btc["after"]["value"] == 150
    eth = next(row for row in derivative if row["after"]["unit"] == "ETH")
    assert eth["before"] is None
    quote = next(row for row in result["changes"] if "/quotes/" in row["path"])
    assert quote["category"] == "receipt_only_update"
    assert quote["after"]["unit"] == "EUR per USDT"
    assert "basis_percent" not in quote["after"]["value"]


def test_book_spread_depth_and_missing_fees_and_collection_failure_are_separate(store):
    wanted = request()
    wanted["dossier"]["markets"][0]["venues"] = {"sources": ["venue"], "purchase_costs": {"budget": "100"}}
    for event, ask in ((PREVIOUS, "101"), (NOW, "103")):
        retain_books(store, [retained_book({"timestamp": event * 1000, "bids": [["99", "2"]], "asks": [[ask, "2"]]},
                                          source="venue", asset="BTC", quote="EUR", symbol="BTC/EUR",
                                          captured_at=event, requested_limit=500, data_mode="import")])
    store.record_run(CollectionResult("venue", errors=["offline"]), "market", NOW, data_mode="import")
    result = build_analysis_changes(store, wanted, NOW)
    spread = change(result, "/venue_disagreement/venue")
    assert spread["before"]["value"]["spread_quote"] == "2"
    assert spread["after"]["value"]["spread_quote"] == "4"
    costs = change(result, "/venue_costs/venue")
    assert costs["after"]["value"]["hypothetical_fill"] is None
    assert costs["after"]["value"]["assumptions"] is None
    assert result["summary"]["collection_errors_after"] == 1
    assert result["summary"]["market_movements"] == 0


def test_exact_omissions_and_shared_candle_budget(store, monkeypatch):
    store.put_candles([candle(), candle(NOW, 110)])
    result = build_analysis_changes(store, request(max_changes=1), NOW)
    assert len(result["changes"]) == 1
    assert result["omissions"]["changes"] == result["summary"]["total_changes"] - 1
    monkeypatch.setattr("crypto_market_analysis.analysis_changes.MAX_TOTAL_CANDLES", 2)
    with pytest.raises(ValueError, match="shared 200000-candle"):
        build_analysis_changes(store, request(), NOW)


@pytest.mark.parametrize("patch", [{"previous_as_of": NOW}, {"previous_as_of": True},
                                  {"max_changes": 0}, {"max_changes": 501}, {"extra": 1}])
def test_request_validation(store, patch):
    with pytest.raises(ValueError):
        build_analysis_changes(store, request(**patch), NOW)


def test_requested_context_return_revision_is_visible_with_independent_numeric_answer(store):
    cutoff = 1000 * 86400
    bars = [candle(event, 100) for event in range(cutoff - 3 * 86400 + 3600, cutoff + 1, 3600)]
    store.put_candles(bars)
    wanted = request(previous_as_of=cutoff)
    wanted["dossier"]["markets"][0]["lookbacks"] = [2]
    revised = next(row for row in bars if row.close_time == cutoff - 2 * 86400)
    store.put_candles([replace(revised, high=200, close=200, observed_at=cutoff + 1)])
    result = build_analysis_changes(store, wanted, cutoff + 1)
    metric = change(result, "/context_metrics/lookbacks/2/return")
    # Independent endpoint calculation: the latest close stays 100, and the
    # earlier daily endpoint is corrected from 100 to 200: 100 / 200 - 1 = -0.5.
    assert metric["before"]["value"] == 0
    assert metric["after"]["value"] == -0.5
    assert metric["before"]["status"] == metric["after"]["status"] == "available"
    assert metric["after"]["unit"] == "fraction"
    assert metric["after"]["formula"] == "last_close / first_close - 1"
    assert metric["after"]["coverage"]["obtained_closes"] == 3
    assert metric["after"]["known_at"] == cutoff + 1
    assert metric["after"]["input_reference_coverage"]["total"] == 72
    assert metric["after"]["input_reference_coverage"]["retained"] == 16
    assert metric["after"]["input_reference_coverage"]["omitted"] == 56
    assert len(metric["after"]["input_references"]) == 16
    assert metric["derived_metric"]
    assert metric["category"] == "same_event_correction"
    assert not metric["market_movement"]
    assert result["summary"]["market_movements"] == 0
    assert result["omissions"]["changes"] == 0


def test_unchanged_context_metrics_ignore_cutoff_and_irrelevant_receipt_changes(store):
    cutoff = 1000 * 86400
    bars = [candle(event, 100) for event in range(cutoff - 3 * 86400 + 3600, cutoff + 1, 3600)]
    store.put_candles(bars)
    wanted = request(previous_as_of=cutoff)
    wanted["dossier"]["markets"][0]["lookbacks"] = [2]
    result = build_analysis_changes(store, wanted, cutoff + 1)
    assert result["changes"] == []
    assert result["snapshots"]["before"]["input_hash"] != result["snapshots"]["after"]["input_hash"]
    # More detailed source evidence changes without altering any calculated
    # metric. Receipt/reference differences alone cannot create metric changes.
    store.put_candles([replace(bars[0], quote_volume=100, observed_at=cutoff + 1)])
    result = build_analysis_changes(store, wanted, cutoff + 1)
    assert not any("/context_metrics/" in row["path"] for row in result["changes"])
    assert result["summary"]["market_movements"] == 0


def test_calendar_metrics_report_normalized_changes_and_window_only_changes(store):
    cutoff = 1000 * 86400
    bars = [candle(event, 100) for event in range(cutoff - 3 * 86400 + 3600, cutoff + 1, 3600)]
    store.put_candles(bars)
    wanted = request(previous_as_of=cutoff)
    revised = next(row for row in bars if row.open_time == cutoff - 86400)
    store.put_candles([replace(revised, open=200, high=200, observed_at=cutoff + 1)])
    result = build_analysis_changes(store, wanted, cutoff + 1)
    metric = change(result, "/context_metrics/calendar/daily/return")
    assert metric["before"]["value"] == 0
    assert metric["after"]["value"] == -0.5
    assert metric["after"]["formula"] == "period_close / period_open - 1"
    assert not metric["market_movement"]
    rollover = build_analysis_changes(store, wanted, cutoff + 86400)
    metric = change(rollover, "/context_metrics/calendar/daily/return")
    assert metric["after"]["value"] is None
    assert metric["after"]["status"] == "unavailable"
    assert metric["category"] == "derived_metric_window_change"
    assert not metric["market_movement"]
