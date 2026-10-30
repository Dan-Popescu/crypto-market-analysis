import hashlib
import json
import sqlite3
import time
from dataclasses import asdict

import pytest

from crypto_market_analysis.collectors import ExchangeClients
from crypto_market_analysis.config import Settings
from crypto_market_analysis.evidence import content_hash
from crypto_market_analysis.history_dataset import DatasetConfig
from crypto_market_analysis.installation_qualification import (
    render_installation_qualification,
    run_installation_qualification,
)
from crypto_market_analysis.local_workflow import NATIVE_MARKETS
from crypto_market_analysis.models import Candle
from crypto_market_analysis.storage import Store


class PublicVenue:
    has = {"fetchOHLCV": True}

    def __init__(self, now, *, markets=NATIVE_MARKETS, blocked=False):
        self.now, self.markets, self.blocked = now, markets, blocked
        self.calls = []

    def load_markets(self):
        self.calls.append("catalogue")
        if self.blocked:
            raise OSError("HTTP 451: public provider host blocked by outbound policy")
        return {market: {"symbol": market, "base": market.split("/")[0],
                         "quote": market.split("/")[1], "spot": True, "active": True}
                for market in self.markets}

    def fetch_ohlcv(self, symbol, timeframe, *, since, limit):
        self.calls.append(symbol)
        return [[(self.now - (24 - hour) * 3600) * 1000, 100, 101, 99, 100, 1]
                for hour in range(24)]


@pytest.fixture
def setup(tmp_path):
    now = int(time.time()) // 3600 * 3600
    return Settings(data_dir=tmp_path / "local-data", venues=["kraken"]), now


def refresh(settings, now, *, venue=None, **request):
    venue = venue or PublicVenue(now)
    with ExchangeClients() as clients:
        clients._clients["kraken"] = venue
        report = run_installation_qualification(settings, {
            "refresh": True, "sources": ["kraken"], "required_history_days": 1,
            "history_days": 2, "max_pages": 1, **request,
        }, clients=clients, clock=lambda: 0)
    return report, venue


def test_missing_store_default_inspection_is_readonly_and_reproducible(setup, monkeypatch):
    settings, now = setup
    monkeypatch.setattr("crypto_market_analysis.installation_qualification.ExchangeClients",
                        lambda: pytest.fail("inspection attempted public collection"))
    report = run_installation_qualification(settings, {}, clock=lambda: now)
    assert report["status"] == "unavailable"
    assert not settings.data_dir.exists()
    assert len(report["markets"]) == 4
    assert all(row["coverage"] == {"status": "unknown"} for row in report["markets"])
    assert report["collection"]["budget"]["http_request_count"] == 0
    assert report["run_manifest"]["request_sha256"] == content_hash(report["request"])
    assert len(report["run_manifest"]["software"]["source_sha256"]) == 8
    json.dumps(report, allow_nan=False)
    reference = report.pop("evidence_reference")
    assert reference == "sha256:" + content_hash(report)


def test_four_native_markets_capture_and_inspect_independently_without_repeated_writes(setup):
    settings, now = setup
    report, venue = refresh(settings, now)
    assert report["as_of"] >= now
    assert report["status"] == "pass"
    assert venue.calls == ["catalogue", *NATIVE_MARKETS]
    assert report["collection"]["budget"]["logical_operations"] == 5
    assert all(row["coverage"]["available_hours"] == 24 for row in report["markets"])
    assert all(row["capture_access"]["status"] == "success" for row in report["markets"])
    assert all(row["capture_access"]["provider_acceptance"] == "unverified_injected_transport"
               for row in report["markets"])
    assert report["resources"]["http_accounting"]["coverage"] == "uninstrumented"
    assert report["resources"]["http_accounting"]["totals"] is None
    assert report["resources"]["adapter_budget"]["logical_operations"] == 5
    store = Store(settings.database, initialize=False)
    before = store.counts()
    for _ in range(2):
        inspected = run_installation_qualification(settings, {"required_history_days": 1})
        assert inspected["status"] == "pass"
        assert not inspected["collection"]["network_requested"]
        assert store.counts() == before
    repeated, _ = refresh(settings, now)
    assert repeated["status"] == "pass"
    assert store.counts()["candles"] == before["candles"]
    assert store.counts()["runs"] > before["runs"]
    assert "BTC/EUR" in render_installation_qualification(repeated)


def test_partial_catalogue_does_not_alias_native_quotes(setup):
    settings, now = setup
    report, venue = refresh(settings, now, venue=PublicVenue(now, markets=["BTC/EUR", "ETH/USD"]))
    assert report["status"] == "partial"
    assert venue.calls == ["catalogue", "BTC/EUR", "ETH/USD"]
    rows = {row["market"]: row for row in report["markets"]}
    assert rows["BTC/EUR"]["status"] == rows["ETH/USD"]["status"] == "pass"
    for market in ("ETH/EUR", "BTC/USD"):
        assert rows[market]["coverage"]["available_hours"] == 0
        assert rows[market]["capture_access"]["status"] == "unknown"
        assert "no active supported spot market" in rows[market]["capture_access"]["warnings"][0]


def test_blocked_network_keeps_actual_errors_and_unknown_provider_access(setup):
    settings, now = setup
    report, _ = refresh(settings, now, venue=PublicVenue(now, blocked=True))
    assert report["status"] == "unavailable"
    assert report["collection"]["network_requested"] is True
    for row in report["markets"]:
        assert row["capture_access"]["status"] == "failed"
        assert row["capture_access"]["last_failure"] >= now
        assert "HTTP 451" in row["capture_access"]["errors"][0]
        assert row["source_access"]["status"] == "untested"
        assert row["rights"]["status"] == "intended_use_review_required"
        assert row["regulatory_eligibility"]["jurisdiction"] == "France"
        assert row["source_health"]["last_failure"] is not None


def test_missing_pair_in_new_capture_cannot_pass_due_to_previously_stored_coverage(setup):
    settings, now = setup
    refresh(settings, now)
    report, _ = refresh(settings, now, venue=PublicVenue(now, markets=["BTC/EUR"]))
    assert report["qualification"]["status"] == "pass"
    assert report["status"] == "partial"
    assert report["markets"][1]["capture_access"]["status"] == "unknown"


def test_recent_capture_cannot_qualify_two_years_or_claim_kraken_pagination_extends_retention(setup):
    settings, now = setup
    report, _ = refresh(settings, now, required_history_days=730)
    assert report["status"] == "partial"
    assert all(row["coverage"]["missing_hours"] == 730 * 24 - 24 for row in report["markets"])
    assert all(row["reasons"] == ["required_hourly_history_missing"] for row in report["markets"])
    assert report["older_history"]["kraken_recent_ohlc_limit_hours"] == 720
    assert report["older_history"]["pagination_extends_kraken_retention"] is False
    assert report["older_history"]["automatic_archive_download"] is False


def test_capture_adapter_budget_is_shared_across_all_four_markets(setup):
    settings, now = setup
    report, venue = refresh(settings, now, max_logical_operations=2)
    assert report["collection"]["budget"]["logical_operations"] == 2
    assert venue.calls == ["catalogue", "BTC/EUR"]
    assert report["status"] == "partial"
    assert report["markets"][0]["status"] == "pass"
    assert all(row["capture_access"]["status"] == "failed" for row in report["markets"][1:])


def test_capture_storage_admission_refuses_before_initializing_or_network(setup):
    settings, now = setup
    report, venue = refresh(settings, now, max_storage_bytes=1)
    assert report["collection"]["status"] == "refused"
    assert report["collection"]["network_requested"] is False
    assert not settings.data_dir.exists()
    assert venue.calls == []


def test_request_cannot_raise_the_configured_storage_capture_ceiling(setup):
    settings, now = setup
    settings.storage_hot_max_bytes = 1
    report, venue = refresh(settings, now, max_storage_bytes=512 * 1024**2)
    assert report["resources"]["storage_admission_ceiling_bytes"] == 1
    assert report["collection"]["status"] == "refused"
    assert not settings.data_dir.exists()
    assert venue.calls == []


@pytest.mark.parametrize("limit", [{"max_query_rows": 1}, {"max_storage_bytes": 1}])
def test_qualification_resource_ceilings_cannot_produce_false_pass(setup, limit):
    settings, now = setup
    refresh(settings, now)
    report = run_installation_qualification(settings, {"required_history_days": 1, **limit})
    assert report["status"] != "pass"
    assert all(row["status"] != "pass" for row in report["markets"][1:])


def test_named_import_dataset_inspection_preserves_identity_receipts_and_revisions(setup):
    settings, now = setup
    store = Store(settings.database)
    config = DatasetConfig("portable-research", str(settings.data_dir.resolve()), now - 86400, now)
    marker = settings.data_dir / "dataset.json"
    marker.write_text(json.dumps(asdict(config)), encoding="utf-8")
    for close in (100, 101):
        store.put_candles([Candle("kraken", "BTC", "EUR", "1h", now - 3600, now,
                                  100, 102, 99, close, 1, now, data_mode="import")])
    before = marker.read_bytes(), settings.database.read_bytes(), store.counts()
    report = run_installation_qualification(settings, {
        "data_mode": "import", "sources": ["kraken"], "markets": ["BTC/EUR"],
        "required_history_days": 1, "as_of": now,
    })
    row = report["markets"][0]
    assert row["coverage"]["revision_rows"] == 1
    assert row["coverage"]["earliest_observed_at"] == now
    assert row["coverage"]["missing_hours"] == 23
    assert report["dataset_lineage"]["name"] == "portable-research"
    assert report["dataset_lineage"]["data_mode"] == "import"
    assert report["run_manifest"]["dataset_identity_sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert before == (marker.read_bytes(), settings.database.read_bytes(), store.counts())
    with pytest.raises(ValueError, match="named research dataset"):
        refresh(settings, now)


def test_incompatible_store_is_reported_without_migration(setup):
    settings, now = setup
    settings.data_dir.mkdir()
    with sqlite3.connect(settings.database) as con:
        con.execute("CREATE TABLE unrelated (id INTEGER)")
    before = settings.database.read_bytes()
    report = run_installation_qualification(settings, {}, clock=lambda: now)
    assert report["status"] == "unavailable"
    assert "without initialization or migration" in report["qualification"]["error"]
    assert settings.database.read_bytes() == before


@pytest.mark.parametrize("payload", [
    {"refresh": True, "as_of": 0}, {"refresh": True, "data_mode": "import"},
    {"refresh": True, "data_mode": "demo"}, {"refresh": "yes"},
    {"issue_assessments": True}, {"capture_books": True}, {"analyses": ["quotes"]},
    {"history_days": 31}, {"max_pages": 5}, {"max_logical_operations": 257},
    {"max_query_rows": 0}, {"markets": ["BTC/USDT"]},
    {"market_sources": {"BTC/EUR": ["unknown"]}},
    {"sources": ["kraken", "coinbase", "binance", "okx", "bybit"]},
])
def test_invalid_request_has_no_write_or_network_side_effects(setup, payload):
    settings, now = setup
    venue = PublicVenue(now)
    with ExchangeClients() as clients:
        clients._clients["kraken"] = venue
        with pytest.raises(ValueError):
            run_installation_qualification(settings, payload, clients=clients)
    assert venue.calls == []
    assert not settings.data_dir.exists()
