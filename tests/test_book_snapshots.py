"""Public book bridge checks use injected realistic CCXT response boundaries."""

from copy import deepcopy

import pytest

from crypto_market_analysis.book_snapshots import (
    disagreement_request,
    purchase_cost_request,
    read_book_snapshot,
    read_books,
    retain_books,
    retained_book,
)
from crypto_market_analysis.collectors import ExchangeClients, capture_books, collect_exchange
from crypto_market_analysis.models import CollectionResult
from crypto_market_analysis.storage import Store
from crypto_market_analysis.venue_analysis import analyze_purchase_costs, analyze_venue_disagreement
from crypto_market_analysis.venues import VENUES


class PublicBook:
    has = {"fetchOrderBook": True}

    def __init__(self, quote="EUR", fail=False):
        self.quote, self.fail = quote, fail
        self.calls = self.catalogues = 0
        self.closed = False

    def load_markets(self, reload=False):
        self.catalogues += 1
        return {"native": {"base": "BTC", "quote": self.quote, "spot": True,
                           "symbol": f"BTC/{self.quote}", "id": f"BTC{self.quote}"}}

    def fetch_order_book(self, symbol, limit):
        self.calls += 1
        assert symbol == f"BTC/{self.quote}" and limit in (100, 500)
        if self.fail:
            raise RuntimeError("source denied access")
        return {"timestamp": 100999, "nonce": 123, "bids": [["99.00", "2.000", 4]],
                "asks": [["101.00", "3.000", 2], [102, 4.25, 1]]}

    def close(self):
        self.closed = True


def book(source="kraken", at=101, event=100999, quote="EUR", mode="live", asks=None):
    return retained_book(
        {"timestamp": event, "bids": [["99.00", "2.000", 4]],
         "asks": asks if asks is not None else [["101.00", "3.000", 2], [102, 4.25, 1]]},
        source=source, asset="BTC", quote=quote, symbol=f"BTC/{quote}", captured_at=at,
        requested_limit=500, data_mode=mode, provenance={"adapter": "ccxt", "market_id": f"BTC{quote}"},
    )


def selectors(as_of=102):
    return {"sources": ["kraken"], "asset": "BTC", "quote": "EUR", "data_mode": "live", "as_of": as_of}


def assumptions():
    return {"fee_rate": "0.01", "fee_step": "0", "fixed_fee_quote": "0", "payment_cost_quote": "0",
            "withdrawal_cost_quote": "0", "withdrawal_cost_base": "0", "quantity_step": "0.001",
            "price_step": "0.01", "min_quantity": "0", "min_notional": "0", "fee_currency": "quote",
            "input_reference": "caller-supplied-fees-and-rules", "fee_tier": "explicit-example"}


def test_opt_in_one_fetch_keeps_existing_metrics_and_positional_contract():
    legacy = CollectionResult("source", [], [], ["warning"], ["error"])
    assert legacy.books == []
    before, after = PublicBook(), PublicBook()
    original = collect_exchange("kraken", ["BTC"], now=101, include=("liquidity",), client=before)
    retained = collect_exchange("kraken", ["BTC"], now=101, include=("liquidity",), client=after,
                                retain_books=True)
    assert before.calls == after.calls == 1
    assert original.observations == retained.observations and original.books == []
    assert retained.books[0]["asks"][0] == ["101.00", "3.000", 2]
    assert retained.books[0]["exchange_timestamp_ms"] == 100999
    assert retained.books[0]["event_time"] == 100
    assert retained.books[0]["coverage"]["whole_book_complete"] is False
    assert not before.closed and not after.closed
    only = collect_exchange("kraken", ["BTC"], now=101, include=(), client=PublicBook(), retain_books=True)
    assert only.books and not only.observations


def test_lossless_roundtrip_idempotence_and_cutoff_versions(tmp_path):
    store = Store(tmp_path / "db")
    original = book()
    assert retain_books(store, [original]) == 1
    assert retain_books(store, [deepcopy(original)]) == 0
    versions = [book(at=105, asks=[["103.00", "2.000"]]), book(at=106, event=110000),
                book(at=105, quote="USD"), book(at=105, mode="demo")]
    assert retain_books(store, versions) == 4
    assert read_books(store, **selectors(104)) == [original]
    assert read_books(store, **selectors(106)) == [versions[0]]
    assert read_books(store, **selectors(110)) == [versions[1]]
    snap = read_book_snapshot(store, **selectors(106))
    assert snap["watermark"] == {"max_eligible_id": 2, "as_of": 106}
    with store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 5


def test_bounds_preserve_received_count_and_do_not_claim_whole_book(tmp_path):
    captured = book(asks=[[100 + n, "1", {"count": n}] for n in range(503)])
    assert len(captured["asks"]) == 500
    assert captured["coverage"]["returned_asks_levels"] == 503
    assert captured["coverage"]["asks_truncated"]
    store = Store(tmp_path / "db")
    retain_books(store, [captured])
    assert read_books(store, **selectors())[0] == captured
    tampered = deepcopy(captured)
    tampered["asks"][0][0] = 2
    with pytest.raises(ValueError, match="inconsistent"):
        retain_books(store, [tampered])


def test_unknown_clock_kept_and_suppressed_without_fabricated_market_time(tmp_path):
    store = Store(tmp_path / "db")
    capture = book(event=None)
    retain_books(store, [capture])
    assert read_books(store, **selectors())[0]["event_time"] is None
    request = purchase_cost_request(store, **selectors(), budget="100")
    assert request["snapshots"] == []
    assert request["book_selection"]["excluded"] == [{"source": "kraken", "reason": "unknown_exchange_event_time"}]
    result = analyze_purchase_costs(request, 102)
    assert result["rows"][0]["status"] == "missing"


def test_bridge_uses_captured_levels_and_explicit_rules_only(tmp_path):
    store = Store(tmp_path / "db")
    retain_books(store, [book()])
    unknown = purchase_cost_request(store, **selectors(), budget="100")
    result = analyze_purchase_costs(unknown, 102)
    assert result["rows"][0]["hypothetical_fill"] is None
    assert "unknown_cost_and_product_rules" in result["rows"][0]["reasons"]
    request = purchase_cost_request(store, **selectors(), budget="100",
                                    assumptions_by_source={"kraken": assumptions()},
                                    product_by_source={"kraken": "spot_order_book"})
    result = analyze_purchase_costs(request, 102)
    row = result["rows"][0]
    assert row["status"] == "hypothetical"
    assert row["hypothetical_fill"]["fills"][0]["price"] == "101"
    assert row["asks_complete"] is False
    assert row["purchase_eligibility"]["status"] == "unknown"
    assert request["book_selection"]["selected"][0]["content_hash"] == book()["content_hash"]
    assert request["book_selection"]["watermark"]["max_eligible_id"] == 1
    shallow = purchase_cost_request(store, **selectors(), budget="100000",
                                    assumptions_by_source={"kraken": assumptions()},
                                    product_by_source={"kraken": "spot_order_book"})
    fill = analyze_purchase_costs(shallow, 102)["rows"][0]["hypothetical_fill"]
    assert not fill["size_relevant_depth"]["budget_supported_by_captured_levels"]


def test_disagreement_two_realistic_native_books_and_missing_venue(tmp_path):
    store = Store(tmp_path / "db")
    retain_books(store, [book(), book(source="coinbase", asks=[["103", "2"]])])
    params = {**selectors(), "sources": ["kraken", "coinbase", "bitvavo"]}
    request = disagreement_request(store, **params)
    result = analyze_venue_disagreement(request, 102)
    assert result["status"] == "diagnostic"
    assert result["range"]["range_quote"] == "1"
    assert result["coverage"]["missing_sources"] == ["bitvavo"]
    assert [r["price"] for r in result["rows"]] == ["100", "101", None]
    with pytest.raises(ValueError, match="native EUR or USD"):
        disagreement_request(store, **{**params, "quote": "USDT"})
    with pytest.raises(ValueError, match="not last"):
        disagreement_request(store, **params, price_basis="last")


@pytest.mark.parametrize("asks", [[["NaN", "1"]], [["101", "-1"]], [["101", "1"], [101, 2]], [["98", "1"]]])
def test_invalid_or_crossed_received_evidence_stays_stored_but_excluded(tmp_path, asks):
    store = Store(tmp_path / "db")
    row = book(asks=asks)
    retain_books(store, [row])
    assert read_books(store, **selectors()) == [row]
    request = disagreement_request(store, **selectors())
    assert request["snapshots"] == [] and request["book_selection"]["excluded"]


def test_capture_reuses_injected_pool_and_isolates_venue_failures(tmp_path):
    store = Store(tmp_path / "db")
    good, bad = PublicBook(), PublicBook(fail=True)
    pool = ExchangeClients()
    pool._clients.update({"kraken": good, "bitstamp": bad})
    for _ in range(2):
        result = capture_books(store, sources=["kraken", "bitstamp"], assets=["BTC"], quotes=("EUR",),
                               now=101, clients=pool)
        assert result["reports"][0]["captured_books"] == 1
        assert "source denied access" in result["reports"][1]["errors"][0]
        assert result["source_access_qualified"] is False
    assert good.calls == bad.calls == 2 and good.catalogues == bad.catalogues == 1
    assert not good.closed and not bad.closed
    assert read_books(store, **selectors())
    assert store.observations("BTC", as_of=102, mode="live")
    assert result["inserted_observations"] == 0  # Repeat evidence stays idempotent.
    pool.close()
    assert good.closed and bad.closed


def test_read_does_not_create_capture_table(tmp_path):
    store = Store(tmp_path / "db")
    assert read_book_snapshot(store, **selectors()) == {
        "books": [], "watermark": {"max_eligible_id": None, "as_of": 102}}
    with store.connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='book_snapshots'").fetchone() is None


def test_book_selection_and_watermark_share_one_sqlite_snapshot(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store = Store(tmp_path / "db")
    other = Store(store.path)
    retain_books(store, [book()])
    connection = store.connection
    injected = []

    @contextmanager
    def instrumented():
        with connection() as con:
            def interleave(sql):
                if "SELECT MAX(id)" in sql and not injected:
                    injected.append(retain_books(other, [book(at=102, asks=[["105", "1"]])]))
            con.set_trace_callback(interleave)
            yield con

    monkeypatch.setattr(store, "connection", instrumented)
    snapshot = read_book_snapshot(store, **selectors(103))
    assert injected == [1]
    assert snapshot["books"] == [book()]
    assert snapshot["watermark"]["max_eligible_id"] == 1
    assert read_book_snapshot(other, **selectors(103))["watermark"]["max_eligible_id"] == 2


def test_zero_exchange_clock_and_after_fetch_receipt_are_preserved(monkeypatch):
    class Clocked(PublicBook):
        def fetch_order_book(self, symbol, limit):
            row = super().fetch_order_book(symbol, limit)
            row["timestamp"] = 0
            return row

    clock = iter([100, 103])
    monkeypatch.setattr("crypto_market_analysis.collectors.time.time", lambda: next(clock))
    result = collect_exchange("kraken", ["BTC"], include=("liquidity",), client=Clocked(), retain_books=True)
    assert result.books[0]["event_time"] == 0
    assert result.books[0]["captured_at"] == 103
    assert result.books[0]["provenance"]["timestamp_source"] == "exchange"


def test_backup_verify_restore_preserves_known_and_unknown_clock_books(tmp_path):
    from crypto_market_analysis.recovery import create_backup, restore_backup, verify_backup

    store = Store(tmp_path / "source" / "db")
    captures = [book(), book(source="coinbase", event=None)]
    retain_books(store, captures)
    bundle = tmp_path / "backup"
    create_backup(store.path, bundle)
    assert verify_backup(bundle)["status"] == "verified"
    result = restore_backup(bundle, tmp_path / "restored")
    assert result["status"] == "restored"
    restored = Store(result["database"])
    params = {**selectors(), "sources": ["kraken", "coinbase"]}
    assert read_books(restored, **params) == captures
    assert read_book_snapshot(restored, **params)["watermark"]["max_eligible_id"] == 2


def test_unknown_clock_does_not_break_other_timed_venues(tmp_path):
    store = Store(tmp_path / "db")
    retain_books(store, [book(), book(source="coinbase", asks=[["103", "1"]]),
                         book(source="bitvavo", event=None)])
    request = disagreement_request(store, **{**selectors(), "sources": ["kraken", "coinbase", "bitvavo"]})
    result = analyze_venue_disagreement(request, 102)
    assert result["status"] == "diagnostic"
    assert result["coverage"]["missing_sources"] == ["bitvavo"]
    assert request["book_selection"]["excluded"][0]["reason"] == "unknown_exchange_event_time"


def test_midpoint_precision_boundary_is_an_explicit_exclusion(tmp_path):
    store = Store(tmp_path / "db")
    row = retained_book({"timestamp": 100000, "bids": [["0.000000000000000001", "1"]],
                         "asks": [["0.000000000000000002", "1"]]}, source="kraken", asset="BTC", quote="EUR",
                        symbol="BTC/EUR", captured_at=101, requested_limit=500)
    retain_books(store, [row])
    request = disagreement_request(store, **selectors())
    assert request["snapshots"] == []
    assert "precision" in request["book_selection"]["excluded"][0]["reason"]
    assert analyze_venue_disagreement(request, 102)["status"] == "unsupported"


@pytest.mark.parametrize("source", ["bitvavo", "binance", "kraken"])
def test_configured_large_book_limits_preserve_full_metrics_and_bounded_evidence(tmp_path, source):
    import hashlib
    import json

    limit = VENUES[source].book_limit
    assert limit == {"bitvavo": 1000, "binance": 5000, "kraken": 500}[source]
    raw = {
        "timestamp": 100999,
        "bids": [[f"10000.{9999 - index:04d}", "0.123400", index] for index in range(limit)],
        "asks": [[f"10001.{index:04d}", "0.234500", index] for index in range(limit)],
    }

    class ConfiguredLimitBook(PublicBook):
        def fetch_order_book(self, symbol, limit):
            self.calls += 1
            assert symbol == "BTC/EUR" and limit == len(raw["bids"])
            return deepcopy(raw)

    client = ConfiguredLimitBook()
    legacy = collect_exchange(source, ["BTC"], now=101, include=("liquidity",), quotes=("EUR",), client=client)
    result = collect_exchange(source, ["BTC"], now=101, include=("liquidity",), quotes=("EUR",),
                              client=client, retain_books=True)
    assert client.calls == 2
    assert not result.errors and result.observations == legacy.observations
    full_bid_depth = sum(float(price) * float(quantity) for price, quantity, _ in raw["bids"])
    assert next(row.value for row in result.observations if row.metric == "depth_bid_2pct") == full_bid_depth
    snapshot = result.books[0]
    assert snapshot["requested_limit"] == limit
    for side in ("bids", "asks"):
        assert snapshot[side] == raw[side][:500]
        assert snapshot["coverage"][f"returned_{side}_levels"] == limit
        assert snapshot["coverage"][f"retained_{side}_levels"] == 500
        assert snapshot["coverage"][f"{side}_truncated"] is (limit > 500)
    content = {key: value for key, value in snapshot.items() if key not in ("content_hash", "input_reference")}
    expected_hash = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                              separators=(",", ":")).encode()).hexdigest()
    assert snapshot["content_hash"] == expected_hash
    store = Store(tmp_path / "db")
    pool = ExchangeClients()
    pool._clients[source] = client
    captured = capture_books(store, sources=[source], assets=["BTC"], quotes=("EUR",), now=101, clients=pool)
    assert client.calls == 3
    assert captured["inserted_books"] == 1 and captured["inserted_observations"] == len(result.observations)
    assert captured["reports"][0]["errors"] == []
    restored = read_books(store, **{**selectors(), "sources": [source]})[0]
    assert restored == snapshot
    assert restored["content_hash"] == expected_hash
    pool.close()
