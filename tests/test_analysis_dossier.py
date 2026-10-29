"""Coherent native-market reads use real WAL transactions and existing rules."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from crypto_market_analysis.analysis_dossier import build_analysis_dossier
from crypto_market_analysis.book_snapshots import retain_books, retained_book
from crypto_market_analysis.context import market_context
from crypto_market_analysis.evidence import content_hash
from crypto_market_analysis.models import Candle, Observation
from crypto_market_analysis.operational_reads import upcoming_calendar
from crypto_market_analysis.storage import Store
from crypto_market_analysis.stored_analysis import (
    analyze_stored_derivatives,
    analyze_stored_quotes,
    analyze_stored_venues,
)

NOW = 4 * 86400


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "observer.sqlite")


def candles(asset="BTC", quote="EUR", source="venue", mode="import", price=100):
    return [Candle(source, asset, quote, "1h", t, t + 3600, price, price, price, price, 1,
                   t + 3600, data_mode=mode) for t in range(NOW - 2 * 86400, NOW, 3600)]


def oi(value=100, **changes):
    row = Observation("derivative", "BTC", "open_interest_amount", NOW, NOW, value, "BTC",
                      "USDT", {"symbol": "BTC/USDT:USDT", "settle": "USDT", "market_type": "swap",
                               "counting_convention": "one_sided"}, "import")
    return replace(row, **changes)


def quote(value="1.01", **changes):
    return {"record_id": "stablecoin-price", "source": "quote-source", "base": "USDT", "quote": "EUR",
            "value": value, "event_time": NOW, "observed_at": NOW, "data_mode": "import",
            "source_url": "https://example.test/price", "data_version": "synthetic-v1",
            "price_type": "observed_spot", **changes}


def book(price="101", **changes):
    selectors = {"source": "venue", "asset": "BTC", "quote": "EUR", "symbol": "BTC/EUR",
                 "captured_at": NOW, "requested_limit": 500, "data_mode": "import", **changes}
    return retained_book({"timestamp": NOW * 1000, "bids": [["99", "2"]], "asks": [[price, "2"]]},
                         **selectors)


def request(**changes):
    return {"data_mode": "import", "markets": [{"asset": "BTC", "source": "venue", "quote": "EUR",
            "lookbacks": [1], "venues": {"sources": ["venue"], "purchase_costs": {"budget": "100"}},
            "derivatives": {"source": "derivative", "quote": "USDT"},
            "quotes": {"sources": ["quote-source"]}}], **changes}


def seed(store):
    store.put_candles(candles())
    store.put_observations([oi(event_time=NOW - 1, observed_at=NOW - 1)])
    store.save_local_record("stablecoin_price", quote())
    retain_books(store, [book()])
    store.save_local_record("calendar", {"event_id": "event", "event_time": NOW + 100,
                                        "observed_at": NOW, "source": "calendar", "status": "planned"})


def without_evidence(result):
    return {key: value for key, value in result.items() if key != "stored_inputs"}


def test_calculations_equal_independent_tools_and_all_sections_share_physical_watermark(store):
    seed(store)
    wanted = request()
    frozen = build_analysis_dossier(store, wanted, NOW)
    market = frozen["markets"][0]
    context = without_evidence(market["market_context"])
    context.pop("input_truncation")
    assert context == market_context(candles(), NOW, [1])
    derivative = analyze_stored_derivatives(store, {"asset": "BTC", "source": "derivative", "quote": "USDT",
                                                   "data_mode": "import"}, NOW)
    assert without_evidence(market["derivatives"]) == without_evidence(derivative)
    quotes = analyze_stored_quotes(store, {"asset": "BTC", "sources": ["quote-source"], "quote": "EUR",
                                          "data_mode": "import"}, NOW)
    assert without_evidence(market["quotes"]) == without_evidence(quotes)
    for section, kind in (("venue_disagreement", "disagreement"), ("venue_costs", "purchase_costs")):
        panel = {"analysis": kind, "asset": "BTC", "sources": ["venue"], "quote": "EUR", "data_mode": "import"}
        if kind == "purchase_costs":
            panel["budget"] = "100"
        independent = analyze_stored_venues(store, panel, NOW)
        assert without_evidence(market[section]) == without_evidence(independent)
        assert market[section]["stored_inputs"]["input_hash"] == independent["stored_inputs"]["input_hash"]
    assert frozen["calendar"]["rows"] == upcoming_calendar(store.path, "import", as_of=NOW)["rows"]
    for section in (market["market_context"], market["derivatives"], market["quotes"],
                    market["venue_costs"], market["venue_disagreement"], frozen["calendar"]):
        evidence = section["stored_inputs"]
        assert evidence["watermark"] == frozen["watermark"]
        assert evidence["as_of"] == NOW
        assert evidence["input_hash"]
        assert evidence["input_references"]
    # Missing cost assumptions are not replaced with invented zero fees or eligibility.
    assert market["venue_costs"]["rows"][0]["hypothetical_fill"] is None
    assert market["venue_costs"]["rows"][0]["assumptions"] is None
    assert wanted == request()
    unhashed = {key: value for key, value in frozen.items() if key not in ("input_hash", "snapshot_id")}
    assert frozen["input_hash"] == content_hash(unhashed)


def test_commits_between_sections_cannot_change_later_sections(store, monkeypatch):
    seed(store)
    original = market_context
    inserted = False

    def after_context(*args, **kwargs):
        nonlocal inserted
        result = original(*args, **kwargs)
        if inserted:
            return result
        inserted = True

        def writer():
            # Use an actual independent thread/connection while the read transaction is open.
            live = Store(store.path)
            live.put_observations([oi(200)])
            live.save_local_record("stablecoin_price", quote("1.99"))
            retain_books(live, [book("151")])
            live.save_local_record("calendar", {"event_id": "event", "event_time": NOW + 100,
                                                "observed_at": NOW, "source": "calendar", "status": "cancelled"})
            live.put_candles([replace(candles()[0], open=200, high=200, low=200, close=200, observed_at=NOW)])

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(writer).result(timeout=5)
        return result

    monkeypatch.setattr("crypto_market_analysis.analysis_dossier.market_context", after_context)
    frozen = build_analysis_dossier(store, request(), NOW)
    market = frozen["markets"][0]
    assert market["derivatives"]["series"][0]["current"]["value"] == 100
    assert market["quotes"]["stored_inputs"]["consumed_records"][0]["payload"]["value"] == "1.01"
    assert market["venue_costs"]["stored_inputs"]["consumed_records"][0]["asks"][0]["price"] == "101"
    assert frozen["calendar"]["rows"][0]["status"] == "planned"
    assert frozen["watermark"]["observations"] == 1
    assert frozen["watermark"]["book_snapshots"] == 1
    assert frozen["watermark"]["candles"] == 48
    # A fresh operation sees every newly committed section coherently.
    later = build_analysis_dossier(store, request(), NOW)
    assert later["markets"][0]["derivatives"]["series"][0]["current"]["value"] == 200
    assert later["calendar"]["rows"] == []
    assert later["watermark"]["candles"] == 49
    assert later["snapshot_id"] != frozen["snapshot_id"]
    assert frozen["calendar"]["rows"][0]["status"] == "planned"


def test_read_session_sees_committed_wal_and_never_changes_original_store(store):
    # Hold a connection so the committed new row is still in WAL at read start.
    with store.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO runs(source,task,observed_at,payload) VALUES('venue','market',?,?)",
                       (NOW, '{"source":"venue","task":"market","observed_at":345600,"errors":[]}'))
        writer.commit()
        assert store.path.with_name(store.path.name + "-wal").stat().st_size > 0
        with store.read_session() as session:
            assert session is not store
            assert session.watermark_at_start["runs"] == 1
            with session.connection() as con:
                assert con.in_transaction
                assert con.execute("PRAGMA query_only").fetchone()[0] == 1
                assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    con.execute("DELETE FROM runs")
            assert store.read_only is False
        with pytest.raises(ValueError, match="expired"):
            with session.connection():
                pass
    store.put_observations([oi()])
    assert store.watermark()["observations"] == 1


def test_four_native_markets_modes_sources_known_at_and_independent_derivatives_quote(store):
    for asset in ("BTC", "ETH"):
        for quote_name, price in (("EUR", 100), ("USD", 200)):
            store.put_candles(candles(asset, quote_name, price=price))
    store.put_candles(candles(source="other", price=999) + candles(mode="live", price=999))
    store.put_candles([replace(candles()[-1], observed_at=NOW + 1, close=999, high=999)])
    store.put_observations([oi(), oi(999, quote="EUR"), oi(999, source="other"),
                            oi(999, data_mode="live"), oi(999, observed_at=NOW + 1)])
    markets = [{"asset": asset, "source": "venue", "quote": native, "lookbacks": [1],
                "derivatives": {"source": "derivative", "quote": "USDT"}}
               for asset in ("BTC", "ETH") for native in ("EUR", "USD")]
    frozen = build_analysis_dossier(store, request(markets=markets), NOW)
    assert len(frozen["markets"]) == 4
    for market in frozen["markets"]:
        assert market["market_context"]["anchor"]["price"] == (100 if market["selection"]["quote"] == "EUR" else 200)
        for row in market["market_context"]["stored_inputs"]["consumed_records"]:
            assert all(row[key] == market["selection"][key] for key in ("asset", "source", "quote", "data_mode"))
        derivatives = market["derivatives"]
        assert derivatives["stored_inputs"]["selection"]["quote"] == "USDT"
        assert [row["current"]["value"] for row in derivatives["series"]] == ([100] if market["selection"]["asset"] == "BTC" else [])


def test_calendar_reuses_version_sequence_cancellation_and_unknown_clock_rules(store):
    base = {"event_id": "ecb-event", "event_time": NOW + 100, "source": "ecb", "data_mode": "import",
            "source_format": "ecb-manual-calendar-v1", "sequence": 2, "observed_at": NOW - 2,
            "status": "cancelled"}
    store.save_local_record("calendar", base)
    store.save_local_record("calendar", {**base, "sequence": 1, "status": "planned", "observed_at": NOW - 1})
    store.save_local_record("calendar", {"event_id": "date-only", "source": "ecb", "event_time": None,
                                        "local_date": "1970-01-05", "observed_at": NOW - 1})
    store.save_local_record("calendar", {"event_id": "future-receipt", "source": "ecb", "event_time": NOW + 200,
                                        "observed_at": NOW + 1})
    wanted = request(calendar={"source": "ecb"})
    result = build_analysis_dossier(store, wanted, NOW)
    expected = upcoming_calendar(store.path, "import", source="ecb", as_of=NOW)
    assert result["calendar"]["rows"] == expected["rows"]
    assert [row["event_id"] for row in result["calendar"]["rows"]] == ["date-only"]
    assert result["calendar"]["rows"][0]["event_time"] is None


def test_missing_database_and_schema_are_not_initialized(tmp_path):
    absent = tmp_path / "missing" / "observer.sqlite"
    with pytest.raises(ValueError, match="missing"):
        build_analysis_dossier(Store(absent, initialize=False), request(), NOW)
    assert not absent.parent.exists()
    database = tmp_path / "old.sqlite"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE original(value)")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="without initialization or migration"):
        build_analysis_dossier(Store(database, initialize=False), request(), NOW)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("original",)]


def test_empty_sections_are_missing_and_optional_sections_are_not_requested(store):
    simple = {"data_mode": "demo", "markets": [{"asset": "BTC", "source": "venue", "quote": "EUR"}]}
    frozen = build_analysis_dossier(store, simple, NOW)
    market = frozen["markets"][0]
    assert market["market_context"]["status"] == "unavailable"
    assert market["market_context"]["anchor"] is None
    assert market["venue_disagreement"] == {"status": "not_requested"}
    assert market["venue_costs"] == {"status": "not_requested"}
    assert market["derivatives"] == {"status": "not_requested"}
    assert market["quotes"] == {"status": "not_requested"}
    assert frozen["calendar"]["status"] == "missing"
    assert frozen["calendar"]["stored_inputs"]["consumed_records"] == []
    assert store.counts() == {"candles": 0, "observations": 0, "events": 0, "runs": 0}


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("data_mode"),
    lambda r: r.update(unexpected=True),
    lambda r: r.update(markets=[]),
    lambda r: r.update(markets=r["markets"] * 5),
    lambda r: r.update(markets=r["markets"] * 2),
    lambda r: r["markets"][0].update(quote="USDT"),
    lambda r: r["markets"][0].pop("source"),
    lambda r: r["markets"][0]["derivatives"].pop("quote"),
    lambda r: r["markets"][0]["quotes"].update(sources=[str(i) for i in range(17)]),
    lambda r: r["markets"][0]["venues"]["purchase_costs"].pop("budget"),
    lambda r: r["markets"][0]["venues"]["purchase_costs"].update(assumptions_by_source={"other": {}}),
    lambda r: r["markets"][0].update(lookbacks=[1] * 17),
    lambda r: r.update(calendar={"cursor": "bad"}),
    lambda r: r["markets"][0]["venues"]["purchase_costs"].update(assumptions_by_source={"venue": {"raw": "x" * 65536}}),
])
def test_invalid_or_excessive_requests_fail_without_opening_database(tmp_path, mutate):
    wanted = request()
    mutate(wanted)
    database = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError):
        build_analysis_dossier(Store(database, initialize=False), wanted, NOW)
    assert not database.exists()


def test_calendar_and_total_candle_excess_fail_without_silent_caps(store, monkeypatch):
    for index in range(101):
        store.save_local_record("calendar", {"event_id": str(index), "event_time": NOW + index,
                                            "observed_at": NOW, "source": "calendar"})
    with pytest.raises(ValueError, match="calendar exceeds 100"):
        build_analysis_dossier(store, request(), NOW)
    store.put_candles(candles())
    monkeypatch.setattr("crypto_market_analysis.analysis_dossier.MAX_CANDLES", 47)
    with pytest.raises(ValueError, match="47-row capture limit"):
        build_analysis_dossier(store, request(), NOW)


def test_result_budget_fails_without_returning_partial_data(store, monkeypatch):
    seed(store)
    monkeypatch.setattr("crypto_market_analysis.analysis_dossier.MAX_RESULT_BYTES", 500)
    with pytest.raises(ValueError, match="complete-result budget"):
        build_analysis_dossier(store, request(), NOW)


def test_hashes_are_deterministic_and_result_detaches_from_request(store):
    seed(store)
    wanted = request()
    before = deepcopy(wanted)
    first = build_analysis_dossier(store, wanted, NOW)
    assert first == build_analysis_dossier(store, wanted, NOW)
    wanted["markets"][0]["source"] = "other"
    assert first["request"] == before
    evidence = first["markets"][0]["market_context"]["stored_inputs"]
    assert evidence["consumed_records"] == [asdict(row) for row in candles()]


def test_explicit_cost_assumptions_are_preserved_and_match_independent_costs(store):
    seed(store)
    assumptions = {"input_reference": "synthetic-fees-v1", "fee_tier": "explicit synthetic taker",
                   "fee_rate": "0.01", "fee_currency": "quote", "fee_step": "0", "fixed_fee_quote": "0",
                   "payment_cost_quote": "0", "withdrawal_cost_quote": "0", "withdrawal_cost_base": "0",
                   "quantity_step": "0.001", "price_step": "0.01", "min_quantity": "0", "min_notional": "0"}
    costs = {"budget": "100", "assumptions_by_source": {"venue": assumptions},
             "product_by_source": {"venue": "spot_order_book"}}
    wanted = request()
    wanted["markets"][0]["venues"]["purchase_costs"] = costs
    dossier = build_analysis_dossier(store, wanted, NOW)
    result = dossier["markets"][0]["venue_costs"]
    baseline = analyze_stored_venues(store, {"analysis": "purchase_costs", "asset": "BTC", "quote": "EUR",
                                              "data_mode": "import", "sources": ["venue"], **costs}, NOW)
    assert without_evidence(result) == without_evidence(baseline)
    assert result["rows"][0]["hypothetical_fill"]["trading_fee_quote"] != "0"
    assert result["rows"][0]["assumptions"] == assumptions
    assert result["rows"][0]["purchase_eligibility"]["status"] == "unknown"


def test_shared_store_supports_independent_simultaneous_read_sessions(store):
    seed(store)
    expected = build_analysis_dossier(store, request(), NOW)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(build_analysis_dossier, store, request(), NOW) for _ in range(2)]
        assert all(job.result(timeout=5) == expected for job in jobs)
    assert store.read_only is False


@pytest.mark.parametrize("cutoff", [True, -1, 2**80])
def test_invalid_cutoffs_fail_before_reading_database(tmp_path, cutoff):
    with pytest.raises(ValueError):
        build_analysis_dossier(Store(tmp_path / "absent.sqlite", initialize=False), request(), cutoff)


def test_read_session_bounds_actual_sqlite_vm_work(store):
    with pytest.raises(ValueError, match="50-million SQLite VM-step budget"):
        with store.read_session() as session, session.connection() as con:
            con.execute("""WITH RECURSIVE rows(n) AS (VALUES(0) UNION ALL
                SELECT n+1 FROM rows WHERE n<100000000) SELECT SUM(n) FROM rows""").fetchone()
    # A refused oversized read cannot poison the original writer.
    store.put_observations([oi()])


def test_read_session_bounds_actual_returned_bytes_and_single_values(store):
    with pytest.raises(ValueError, match="64 MiB returned-data"):
        with store.read_session() as session, session.connection() as con:
            con.execute("""WITH RECURSIVE rows(n) AS (VALUES(0) UNION ALL
                SELECT n+1 FROM rows WHERE n<65) SELECT zeroblob(1048576) FROM rows""").fetchall()
    with pytest.raises(ValueError, match="too big"):
        with store.read_session() as session, session.connection() as con:
            con.execute("SELECT zeroblob(2097153)").fetchone()


def test_read_session_lifetime_is_enforced_without_sleep(store, monkeypatch):
    with store.read_session() as session:
        started = session._started
        monkeypatch.setattr("crypto_market_analysis.storage.time.monotonic", lambda: started + 16)
        with pytest.raises(ValueError, match="15-second lifetime"):
            with session.connection():
                pass
        # Restore before context exit, which independently validates elapsed time.
        monkeypatch.undo()
