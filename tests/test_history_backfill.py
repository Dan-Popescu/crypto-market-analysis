"""Offline native adapter contracts and durable oldest-gap resume behavior."""
from dataclasses import replace

import pytest

from crypto_market_analysis import history_backfill as backfill
from crypto_market_analysis.dataset_lock import DatasetBusy, store_writer_lock
from crypto_market_analysis.models import Candle
from crypto_market_analysis.storage import Store

START = 1704067200
NOW = START + 100 * 3600


def request(source="coinbase", **kwargs):
    return {"source": source, "markets": [{"asset": "BTC", "quote": "EUR"}],
            "start": START, "end": START + 4 * 3600, **kwargs}


def row(stamp):
    return [stamp * 1000, 10, 12, 9, 11, 3]


class Client:
    has = {"fetchOHLCV": True}

    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior

    def fetch_ohlcv(self, symbol, timeframe, since, limit, params):
        self.calls.append((symbol, timeframe, since, limit, params))
        if self.behavior:
            return self.behavior(symbol, since // 1000, limit)
        return [row(since // 1000 + index * 3600) for index in range(limit)]


class Pool:
    def __init__(self, client=None, markets=None):
        self.adapter = client or Client()
        self.catalogue_calls = 0
        self.catalogue = markets if markets is not None else {
            symbol: {"symbol": symbol, "base": symbol.split("/")[0],
                     "quote": symbol.split("/")[1], "spot": True, "active": True}
            for symbol in ("BTC/EUR", "ETH/USD", "BTC/USD", "ETH/EUR")}

    def client(self, source):
        return self.adapter

    def markets(self, source, client):
        self.catalogue_calls += 1
        return self.catalogue


def apply(database, req=None, pool=None, now=NOW):
    return backfill.run_history_backfill(database, req or request(), apply=True,
                                         clients=pool or Pool(), clock=lambda: now)


def test_inspection_never_initializes_or_connects(tmp_path):
    database = tmp_path / "absent" / "market.sqlite"
    pool = Pool()
    result = backfill.run_history_backfill(database, request(), clients=pool, clock=lambda: NOW)
    assert result["markets"][0]["oldest_missing"] == START
    assert backfill.history_backfill_status(database)["status"] == "missing"
    assert not database.parent.exists()
    assert pool.catalogue_calls == 0


@pytest.mark.parametrize("source,cap", [("coinbase", 300), ("bitstamp", 1000)])
def test_native_contract_and_idempotent_receipts(tmp_path, source, cap):
    database = tmp_path / "market.sqlite"
    pool = Pool()
    result = apply(database, request(source), pool)
    assert result["status"] == "complete"
    symbol, timeframe, since, limit, params = pool.adapter.calls[0]
    assert (symbol, timeframe, since, limit) == ("BTC/EUR", "1h", START * 1000, 4)
    assert params["until"] == (START + 4 * 3600) * 1000 - 1
    assert result["source_contract"]["page_cap"] == cap
    with Store(database).connection() as con:
        rows = con.execute("SELECT source,quote,data_mode,observed_at FROM candles").fetchall()
        assert all(tuple(row) == (source, "EUR", "import", NOW) for row in rows)
        assert con.execute("SELECT count(*) FROM local_records WHERE kind='history_import'").fetchone()[0] == 1
    calls = len(pool.adapter.calls)
    assert apply(database, request(source), pool)["cycle"]["logical_requests"] == 0
    assert len(pool.adapter.calls) == calls
    status = backfill.history_backfill_status(database)
    measurement = status["jobs"][0]["markets"]["BTC/EUR"]["http_accounting"]
    assert measurement["coverage"] == "uninstrumented"
    assert measurement["totals"] is None


def test_old_gap_not_hidden_by_newer_candle_or_other_mode(tmp_path):
    database = tmp_path / "market.sqlite"
    store = Store(database)
    candle = Candle("coinbase", "BTC", "EUR", "1h", START + 3 * 3600, START + 4 * 3600,
                    10, 12, 9, 11, 3, NOW, data_mode="import")
    store.put_candles([candle, replace(candle, open_time=START, close_time=START + 3600, data_mode="live")])
    pool = Pool()
    result = apply(database, pool=pool)
    assert result["status"] == "complete"
    assert pool.adapter.calls[0][2:4] == (START * 1000, 3)


@pytest.mark.parametrize("behavior", [lambda s, t, n: [],
    lambda s, t, n: [row(t + 3600)],
    lambda s, t, n: [row(t), row(t)],
    lambda s, t, n: [row(t - 3600)],
    lambda s, t, n: [[t * 1000, 10, 8, 9, 11, 3]],
    lambda s, t, n: [[t * 1000 + 1, 10, 12, 9, 11, 3]]])
def test_bad_or_partial_pages_remain_unknown_and_back_off(tmp_path, behavior):
    database = tmp_path / "market.sqlite"
    pool = Pool(Client(behavior))
    result = apply(database, pool=pool)
    assert result["status"] == "incomplete"
    assert result["cycle"]["pages"] == 1
    assert backfill.history_backfill_status(database)["jobs"][0]["markets"]["BTC/EUR"]["status"] == "unknown_gap"
    calls = len(pool.adapter.calls)
    assert apply(database, pool=pool, now=NOW + 1)["status"] == "deferred"
    assert len(pool.adapter.calls) == calls


def test_commit_before_checkpoint_resume_uses_actual_coverage(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    pool = Pool()
    original = backfill._save_job

    def crash(store, job):
        if any(state.get("status") == "captured" for state in job["markets"].values()):
            raise KeyboardInterrupt
        return original(store, job)

    monkeypatch.setattr(backfill, "_save_job", crash)
    with pytest.raises(KeyboardInterrupt):
        apply(database, pool=pool)
    monkeypatch.setattr(backfill, "_save_job", original)
    result = apply(database, pool=pool)
    assert result["status"] == "complete"
    assert len(pool.adapter.calls) == 1


def test_small_cycles_rotate_and_fail_independently(tmp_path):
    database = tmp_path / "market.sqlite"
    def behavior(symbol, stamp, count):
        if symbol == "BTC/EUR":
            raise RuntimeError("secret provider content")
        return [row(stamp)]
    pool = Pool(Client(behavior))
    req = request(markets=[{"asset": "BTC", "quote": "EUR"}, {"asset": "ETH", "quote": "USD"}],
                  max_pages=1, max_requests=2, max_rows=1)
    first = apply(database, req, pool)
    second = apply(database, req, pool, now=NOW + 1)
    assert first["cycle"]["attempts"][0]["market"] == "BTC/EUR"
    assert second["cycle"]["attempts"][0]["market"] == "ETH/USD"
    assert "secret" not in str(first)


def test_catalogue_missing_pair_is_explicit(tmp_path):
    database = tmp_path / "market.sqlite"
    result = apply(database, pool=Pool(markets={"OTHER": {"base": "BTC", "quote": "USDT", "spot": True}}))
    assert result["status"] == "incomplete"
    assert backfill.history_backfill_status(database)["jobs"][0]["markets"]["BTC/EUR"]["status"] == "unsupported"


def test_named_dataset_refused_without_mutation(tmp_path):
    (tmp_path / "dataset.json").write_text("{}")
    database = tmp_path / "market.sqlite"
    for operation in [lambda: apply(database), lambda: backfill.plan_history_backfill(database, request()),
                      lambda: backfill.history_backfill_status(database)]:
        with pytest.raises(ValueError, match="named history"):
            operation()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["dataset.json"]


def test_store_lock_is_failfast_and_persistent(tmp_path):
    database = tmp_path / "store" / "market.sqlite"
    with store_writer_lock(database, create=True):
        with pytest.raises(DatasetBusy):
            with store_writer_lock(database, create=True):
                pass
        result = apply(database)
        assert result["status"] == "busy"
        assert not database.exists()
    assert (database.parent / ".history-writer.lock").exists()
    assert apply(database)["status"] == "complete"


def test_status_bounds_and_omissions(tmp_path):
    database = tmp_path / "market.sqlite"
    apply(database)
    apply(database, request("bitstamp"))
    status = backfill.history_backfill_status(database, limit=1)
    assert len(status["jobs"]) == 1
    assert (status["total_jobs"], status["omitted_jobs"], status["truncated"]) == (2, 1, True)


@pytest.mark.parametrize("changes", [{"source": "kraken"}, {"mode": "live"}, {"start": START + 1},
    {"end": START}, {"max_pages": 0}, {"max_rows": True}, {"path": "/tmp/evil"},
    {"markets": [{"asset": "BTC", "quote": "USDT"}]}])
def test_request_validation(changes):
    with pytest.raises(ValueError):
        backfill.normalize_backfill_request(request(**changes))


@pytest.mark.parametrize("source", ["coinbase", "bitstamp"])
def test_actual_ccxt_adapter_uses_bounded_public_route_offline(tmp_path, source, monkeypatch):
    import ccxt

    adapter = getattr(ccxt, source)()
    adapter.set_markets([{"id": "BTC-EUR" if source == "coinbase" else "btceur", "symbol": "BTC/EUR",
                         "base": "BTC", "quote": "EUR", "baseId": "BTC", "quoteId": "EUR",
                         "spot": True, "type": "spot", "active": True}])
    captured = []

    def endpoint(params):
        captured.append(params)
        if source == "coinbase":
            return {"candles": [{"start": str(START + index * 3600), "open": "10", "high": "12",
                                 "low": "9", "close": "11", "volume": "3"} for index in range(4)]}
        return {"data": {"ohlc": [{"timestamp": str(START + index * 3600), "open": "10", "high": "12",
                                   "low": "9", "close": "11", "volume": "3"} for index in range(4)]}}

    endpoint_name = "v3PublicGetBrokerageMarketProductsProductIdCandles" if source == "coinbase" else "publicGetOhlcPair"
    monkeypatch.setattr(adapter, endpoint_name, endpoint)
    pool = Pool(client=adapter, markets=adapter.markets)
    result = apply(tmp_path / "market.sqlite", request(source), pool)
    assert result["status"] == "complete"
    assert captured[0]["start"] in (START, str(START))
    assert int(captured[0]["end"]) == START + 4 * 3600 - 1
    assert len(captured) == 1
    adapter.close()


def test_catalogue_failure_is_retained_and_deferred(tmp_path):
    pool = Pool(markets={})
    database = tmp_path / "market.sqlite"
    result = apply(database, pool=pool)
    assert result["cycle"]["logical_requests"] == 1
    assert result["catalogue_checkpoint"]["status"] == "error"
    assert apply(database, pool=pool, now=NOW + 1)["status"] == "deferred"
    assert pool.catalogue_calls == 1
    with Store(database).connection() as con:
        assert con.execute("SELECT count(*) FROM runs WHERE task='history_backfill_catalogue'").fetchone()[0] == 1


def test_future_range_is_unknown_without_network(tmp_path):
    database = tmp_path / "market.sqlite"
    pool = Pool()
    result = apply(database, request(start=NOW + 3600, end=NOW + 3 * 3600), pool)
    assert result["status"] == "unclosed"
    assert result["markets"][0]["unclosed_hours"] == 2
    assert not database.exists()
    assert pool.catalogue_calls == 0


def test_row_page_and_byte_admission_budgets(tmp_path):
    database = tmp_path / "market.sqlite"
    pool = Pool()
    result = apply(database, request(max_pages=1, max_rows=2, max_requests=2), pool)
    assert result["cycle"]["logical_requests"] == 2
    assert result["cycle"]["pages"] == 1
    assert result["cycle"]["rows"] == 2
    assert pool.adapter.calls[0][3] == 2
    second = apply(database, request(max_bytes=1), pool, now=NOW + 1)
    assert second["cycle"]["pages"] == 1
    assert second["cycle"]["rows"] == 0
    assert second["markets"][0]["missing_hours"] == 2


def test_retry_after_is_durable_and_uses_no_sleep(tmp_path):
    class Throttled(RuntimeError):
        headers = {"Retry-After": "1200"}
    def behavior(symbol, stamp, count):
        raise Throttled("provider payload secret")
    database = tmp_path / "market.sqlite"
    result = apply(database, pool=Pool(Client(behavior)))
    checkpoint = result["markets"][0]["checkpoint"]
    assert checkpoint["next_retry"] == NOW + 1200
    assert checkpoint["error"] == f"Throttled retry_at={float(NOW + 1200)}"


def test_injected_value_error_never_retains_provider_message(tmp_path):
    def behavior(symbol, stamp, count):
        raise ValueError("https://provider.invalid?secret=provider-body")
    result = apply(tmp_path / "market.sqlite", pool=Pool(Client(behavior)))
    assert result["markets"][0]["checkpoint"]["error"] == "ValueError"
    assert "provider-body" not in str(result)


def test_midcycle_hard_http_limit_stops_other_markets(tmp_path):
    from crypto_market_analysis.http_limits import current_http_budget
    def behavior(symbol, stamp, count):
        budget = current_http_budget()
        budget.admit()
        budget.admit()  # Simulate a native adapter's additional transport attempt.
        return [row(stamp)]
    pool = Pool(Client(behavior))
    req = request(markets=[{"asset": "BTC", "quote": "EUR"}, {"asset": "ETH", "quote": "USD"}],
                  http_max_requests=1)
    result = apply(tmp_path / "market.sqlite", req, pool)
    assert len(pool.adapter.calls) == 1
    assert result["cycle"]["pages"] == 1
    assert result["cycle"]["http_budget"]["used_requests"] == 1
    assert result["cycle"]["http_budget"]["refused_requests"] == 1


def test_status_mode_mismatch_is_structured_and_noninitializing(tmp_path):
    database = tmp_path / "absent" / "market.sqlite"
    assert backfill.history_backfill_status(database, mode="demo")["status"] == "selection_mismatch"
    assert not database.parent.exists()


@pytest.mark.parametrize("owned", [False, True])
def test_capture_origin_is_explicit_in_job_checkpoint_and_page_record(tmp_path, monkeypatch, owned):
    import json

    pool = Pool()
    if owned:
        # Exercise the owned-adapter branch without permitting public network.
        monkeypatch.setattr(pool, "close", lambda: None, raising=False)
        monkeypatch.setattr(backfill, "ExchangeClients", lambda: pool)
    database = tmp_path / "market.sqlite"
    backfill.run_history_backfill(database, request(), apply=True,
                                  clients=None if owned else pool, clock=lambda: NOW)
    origin = "public_backfill_adapter" if owned else "injected_clients_unqualified"
    job = backfill.history_backfill_status(database)["jobs"][0]
    assert job["capture_evidence"] == origin
    checkpoint = job["markets"]["BTC/EUR"]
    assert checkpoint["capture_evidence"] == origin
    assert checkpoint["captured_at"] == NOW
    assert checkpoint["validated_rows"] == 4
    with Store(database).connection() as con:
        payload = json.loads(con.execute("SELECT payload FROM local_records WHERE kind='history_import'").fetchone()[0])
    assert payload["capture_evidence"] == origin
    assert payload["validated_rows"] == payload["rows"] == 4
    assert payload["captured_at"] == payload["observed_at"] == NOW
    assert (payload["source"], payload["asset"], payload["quote"], payload["data_mode"]) == (
        "coinbase", "BTC", "EUR", "import")
    assert payload["raw_evidence_retained"] is False
    assert payload["normalized_response_hash_basis"] == "ccxt OHLCV rows"


def test_empty_page_provenance_cannot_claim_nonempty_capture(tmp_path):
    import json

    database = tmp_path / "market.sqlite"
    apply(database, pool=Pool(Client(lambda symbol, stamp, count: [])))
    with Store(database).connection() as con:
        payload = json.loads(con.execute("SELECT payload FROM local_records WHERE kind='history_import'").fetchone()[0])
    assert payload["validated_rows"] == 0
    assert payload["capture_evidence"] == "injected_clients_unqualified"


@pytest.mark.parametrize("phase", ["catalogue", "page"])
@pytest.mark.parametrize("metadata", ["seconds", "date", "absolute", "client_header"])
def test_retry_deadline_survives_restart_and_preserves_native_independence(tmp_path, phase, metadata):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    class Throttled(RuntimeError):
        pass

    exception = Throttled("https://provider.invalid?secret=header-or-body")
    deadline = NOW + 300
    date = format_datetime(datetime.fromtimestamp(deadline, UTC), usegmt=True)
    if metadata == "seconds":
        exception.headers = {"Retry-After": "300"}
    elif metadata == "date":
        exception.headers = {"Retry-After": date}
    elif metadata == "absolute":
        exception.retry_at = deadline

    def behavior(symbol, stamp, count):
        if symbol == "BTC/EUR":
            raise exception
        return [row(stamp + index * 3600) for index in range(count)]

    pool = Pool(Client(behavior))
    if metadata == "client_header":
        pool.adapter.last_response_headers = {"Retry-After": date, "Authorization": "never-retain-header"}
    if phase == "catalogue":
        def fail_catalogue(source, client):
            pool.catalogue_calls += 1
            raise exception
        pool.markets = fail_catalogue
    database = tmp_path / "market.sqlite"
    req = request(markets=[{"asset": "BTC", "quote": "EUR"}, {"asset": "ETH", "quote": "USD"}])
    first = apply(database, req, pool)
    checkpoint = (first["catalogue_checkpoint"] if phase == "catalogue"
                  else first["markets"][0]["checkpoint"])
    assert checkpoint["next_retry"] == deadline
    assert checkpoint["retry_deadline_basis"] == {
        "seconds": "exception_response_retry_after", "date": "exception_response_retry_after",
        "absolute": "exception_retry_at", "client_header": "ccxt_client_last_response_header_unverified"}[metadata]
    if phase == "page":
        assert first["markets"][1]["missing_hours"] == 0
    assert "header-or-body" not in str(first)
    assert "never-retain-header" not in str(first)
    restarted = Pool()
    assert apply(database, req, restarted, now=deadline - 1)["status"] == "deferred"
    assert restarted.catalogue_calls == 0
    assert restarted.adapter.calls == []
    resumed = apply(database, req, restarted, now=deadline)
    assert resumed["status"] == "complete"
    if phase == "page":
        assert [call[0] for call in restarted.adapter.calls] == ["BTC/EUR"]


@pytest.mark.parametrize("phase", ["catalogue", "page"])
def test_expired_retry_header_uses_local_exponential_minimum(tmp_path, phase):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    class Expired(RuntimeError):
        headers = {"Retry-After": format_datetime(datetime.fromtimestamp(NOW - 10, UTC), usegmt=True)}

    def fail(symbol, stamp, count):
        raise Expired("never-retain-provider-message")

    pool = Pool(Client(fail))
    # A stale client header must not override an explicit current response's
    # expired header; the local minimum wins instead.
    pool.adapter.last_response_headers = {"Retry-After": "86400"}
    if phase == "catalogue":
        def fail_catalogue(source, client):
            pool.catalogue_calls += 1
            raise Expired("never-retain-provider-message")
        pool.markets = fail_catalogue
    database = tmp_path / "market.sqlite"
    first = apply(database, pool=pool)
    checkpoint = (first["catalogue_checkpoint"] if phase == "catalogue"
                  else first["markets"][0]["checkpoint"])
    assert checkpoint["next_retry"] == NOW + 60
    assert checkpoint["retry_deadline_basis"] == "local_exponential_backoff"
    second = apply(database, pool=pool, now=NOW + 60)
    checkpoint = (second["catalogue_checkpoint"] if phase == "catalogue"
                  else second["markets"][0]["checkpoint"])
    assert checkpoint["next_retry"] == NOW + 60 + 120
    assert checkpoint["failures"] == 2
    assert "never-retain-provider-message" not in str(second)


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "x" * 129, True, "not a date"])
def test_invalid_retry_metadata_is_bounded_and_cannot_shorten_backoff(value):
    class Invalid(RuntimeError):
        headers = {"Retry-After": value}

    assert backfill._retry_deadline(Invalid(), NOW, 60) == (NOW + 60, "local_exponential_backoff")
