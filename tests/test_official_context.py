"""Synthetic documented-schema cases; no real HTTP or exchange requests."""

import hashlib
from datetime import UTC, datetime

import pytest

from crypto_market_analysis.official_context import (
    ECB_FX_URL,
    OfficialHTTP,
    capture_official_context,
    parse_ecb_fx,
)


def epoch(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())


NOW = epoch("2026-10-29T13:00:00")


def xml(day="2026-10-28", rows='<Cube currency="USD" rate="1.1234"/>'):
    return (
        '<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
        'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
        '<gesmes:subject>Synthetic reference rates fixture</gesmes:subject>'
        f'<Cube><Cube time="{day}">{rows}</Cube></Cube></gesmes:Envelope>'
    ).encode()


class HTTP:
    def __init__(self, payload=None):
        self.payload = xml() if payload is None else payload
        self.calls = []

    def get_bytes(self, url):
        self.calls.append(url)
        return self.payload


class Exchange:
    has = {"fetchTicker": True}
    urls = {"www": "https://www.kraken.com"}

    def __init__(self, timestamp=NOW * 1000, last="0.9999"):
        self.timestamp, self.last, self.calls = timestamp, last, []

    def fetch_ticker(self, symbol):
        self.calls.append(symbol)
        return {"symbol": symbol, "timestamp": self.timestamp, "last": self.last}


class Pool:
    def __init__(self, exchange=None, markets=None):
        self.exchange = exchange or Exchange()
        self.catalogue = markets if markets is not None else {
            "USDC/USD": {"base": "USDC", "quote": "USD", "spot": True, "active": True, "symbol": "USDC/USD"},
            "USDT/EUR": {"base": "USDT", "quote": "EUR", "spot": True, "active": True, "symbol": "USDT/EUR"},
            "USDT/USDC": {"base": "USDT", "quote": "USDC", "spot": True, "symbol": "USDT/USDC"},
            "USDC/USD:USD": {"base": "USDC", "quote": "USD", "spot": False, "symbol": "USDC/USD:USD"},
        }
        self.calls = []

    def client(self, venue):
        self.calls.append(venue)
        return self.exchange

    def markets(self, venue, exchange):
        return self.catalogue


def test_fx_daily_orientation_date_only_and_exact_bytes_provenance():
    raw = b'\xef\xbb\xbf' + xml()
    row = parse_ecb_fx(raw, NOW)
    assert (row["base"], row["quote"], row["value"]) == ("EUR", "USD", "1.1234")
    assert row["source_date"] == "2026-10-28"
    assert row["event_time"] is None and row["publication_time"] is None
    assert row["source_timezone"] == "Europe/Berlin" and row["timestamp_status"] == "date_only"
    assert row["price_type"] == "indicative_reference" and not row["executable"]
    assert row["observed_at"] == row["materialized_at"] == NOW
    assert row["historical_known_at"] == "unknown"
    assert row["raw_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("raw", [
    xml(rows='<Cube currency="USD" rate="0"/>'),
    xml(rows='<Cube currency="USD" rate="NaN"/>'),
    xml(rows='<Cube currency="EUR" rate="1"/>'),
    xml(rows='<Cube currency="USD" rate="1"/><Cube currency="USD" rate="2"/>'),
    xml(day="2026-10-30"), xml(day="2026-02-30"), xml(day="20261028"),
    b'<!DOCTYPE test [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + xml(),
    xml().replace(b"<Cube>", b"<Cube unexpected='1'>", 1),
    xml().replace(b"</Cube></gesmes", b"</Cube><Cube/></gesmes"),
    b"x" * (2 * 1024 * 1024 + 1),
    xml(rows="<Cube currency='USD' rate='1'/>" + "<Cube/>" * 256),
    xml().decode().encode("utf-16"),
])
def test_fx_malformed_unsupported_unsafe_and_unbounded_xml_fails_closed(raw):
    with pytest.raises((ValueError, UnicodeError)):
        parse_ecb_fx(raw, NOW)


def test_fx_civil_date_is_frankfurt_date_near_utc_midnight():
    capture = epoch("2026-07-01T22:30:00")
    assert parse_ecb_fx(xml(day="2026-07-02"), capture)["source_date"] == "2026-07-02"


def test_public_capture_keeps_native_pairs_and_receipt_hash_scopes():
    http, pool = HTTP(), Pool()
    result = capture_official_context(now=NOW, client=http, clients=pool)
    assert result["status"] == "available" and not result["errors"]
    assert result["logical_requests"] == 4
    assert http.calls == [ECB_FX_URL]
    assert set(pool.exchange.calls) == {"USDC/USD", "USDT/EUR"}
    assert len(result["stablecoin_prices"]) == 2
    assert result["receipts"][0]["hash_scope"] == "response_bytes"
    for row in result["stablecoin_prices"]:
        assert row["price_type"] == "observed_spot" and row["event_time"] == NOW
        assert row["hash_scope"] == "normalized_adapter_payload"
        assert row["data_version"] == "sha256:" + row["raw_sha256"]
        assert row["quote"] in {"EUR", "USD"}


def test_unknown_market_timestamp_stays_unknown_and_future_is_rejected():
    missing = capture_official_context(now=NOW, clients=Pool(Exchange(timestamp=None)), include=("stablecoins",))
    assert all(row["event_time"] is None and row["timestamp_status"] == "unknown"
               for row in missing["stablecoin_prices"])
    future = capture_official_context(now=NOW, clients=Pool(Exchange(timestamp=NOW * 1000 + 1)), include=("stablecoins",))
    assert future["status"] == "unavailable" and len(future["errors"]) == 2
    assert future["stablecoin_prices"] == []
    assert len(future["receipts"]) == 2  # Raw evidence survives failed normalization.


def test_request_budget_and_per_source_partial_failure():
    limited = capture_official_context(now=NOW, client=HTTP(), clients=Pool(), max_requests=3)
    assert limited["status"] == "partial" and limited["logical_requests"] == 3
    assert len(limited["stablecoin_prices"]) == 1 and limited["errors"]
    invalid = capture_official_context(now=NOW, client=HTTP(b"bad xml"), clients=Pool())
    assert invalid["status"] == "partial" and invalid["fx_reference"] is None
    assert invalid["errors"][0]["source"] == "ecb"
    assert invalid["receipts"][0]["raw_sha256"] == hashlib.sha256(b"bad xml").hexdigest()


@pytest.mark.parametrize("params", [
    {"data_mode": "import"}, {"data_mode": "demo"}, {"now": -1}, {"max_requests": 0},
    {"max_requests": 33}, {"venues": ("unknown",)}, {"include": ("other",)},
])
def test_invalid_request_rejected_before_external_access(params):
    http, pool = HTTP(), Pool()
    with pytest.raises(ValueError):
        capture_official_context(**{"now": NOW, "client": http, "clients": pool, **params})
    assert not http.calls and not pool.calls


def test_live_clock_materializes_after_slow_response(monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr("crypto_market_analysis.official_context.time.time", lambda: clock["now"])

    class SlowHTTP(HTTP):
        def get_bytes(self, url):
            clock["now"] += 20
            return super().get_bytes(url)

    result = capture_official_context(client=SlowHTTP(), include=("fx",))
    assert result["fx_reference"]["observed_at"] == NOW + 20
    assert result["receipts"][0]["captured_at"] == NOW + 20


def test_opaque_transport_errors_are_redacted():
    class BrokenHTTP:
        def get_bytes(self, url):
            raise OSError("https://secret:password@proxy.invalid")

    result = capture_official_context(now=NOW, client=BrokenHTTP(), include=("fx",))
    assert result["status"] == "unavailable"
    assert "secret" not in str(result) and "password" not in str(result)


def test_official_transport_stream_limit_and_redirects_fail_closed():
    import httpx

    http = OfficialHTTP()
    http.client.close()
    http.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))))
    try:
        with pytest.raises(ValueError, match="two-megabyte"):
            http.get_bytes(ECB_FX_URL)
    finally:
        http.close()
    http.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://example.invalid"})))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            http.get_bytes(ECB_FX_URL)
    finally:
        http.close()


def test_injected_clients_are_not_closed_and_unsupported_spot_stays_unknown():
    class CloseHTTP(HTTP):
        def close(self):
            raise AssertionError("Injected client remains caller-owned")

    class ClosePool(Pool):
        def close(self):
            raise AssertionError("Injected pool remains caller-owned")

    result = capture_official_context(now=NOW, client=CloseHTTP(), clients=ClosePool(markets={}))
    assert result["status"] == "partial" and result["stablecoin_prices"] == []


def test_adapter_provenance_uses_declared_website_not_invented_host():
    exchange = Exchange()
    exchange.urls = {"www": "https://public.venue.example/markets"}
    result = capture_official_context(now=NOW, clients=Pool(exchange), include=("stablecoins",))
    assert all(row["source_url"] == "https://public.venue.example/markets"
               for row in result["stablecoin_prices"])
    exchange.urls = {"www": "https://secret:password@venue.example"}
    invalid = capture_official_context(now=NOW, clients=Pool(exchange), include=("stablecoins",))
    assert invalid["status"] == "unavailable" and "password" not in str(invalid)
