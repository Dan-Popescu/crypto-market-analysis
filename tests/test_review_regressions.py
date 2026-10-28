import json
import time
from dataclasses import replace

import pytest

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.evidence import archive_evidence, metric_identity, restore_evidence
from crypto_market_analysis.freshness import metric_freshness
from crypto_market_analysis.models import Candle, Observation
from crypto_market_analysis.storage import Store


def test_contract_conventions_remain_distinct_after_storage_migration(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    first = Observation("fixture", "BTC", "open_interest_amount", 3600, 3601,
                        100, "contracts", "USD", {"symbol": "BTCUSD", "contract_size": 1})
    second = replace(first, observed_at=3602, metadata={**first.metadata, "contract_size": 0.01})
    store.put_observations([first, second])
    assert metric_identity(first) != metric_identity(second)
    assert len(store.observations("BTC", as_of=4000)) == 2
    # Recreate the v1 stored dimension to exercise the additive preserving migration.
    with store.connection() as con:
        con.execute("UPDATE observations SET dimension=?", (json.dumps({"quote": "USD", "unit": "contracts", "symbol": "BTCUSD"}),))
        con.execute("PRAGMA user_version=1")
    migrated = Store(path)
    assert len(migrated.observations("BTC", as_of=4000)) == 2
    assert migrated.counts()["observations"] == 2


def test_unknown_provider_event_time_is_not_fresh_market_evidence():
    point = Observation("fixture", "BTC", "spread_bps", 1000, 1000, 2, "bps", "EUR",
                        {"timestamp_source": "observation"})
    freshness = metric_freshness(point, Settings(), 1000)
    assert freshness["status"] == "unknown"
    assert freshness["usable"] is False and freshness["age_seconds"] is None
    assert freshness["ingestion_age_seconds"] == 0
    assert freshness["recorded_event_time"] == 1000


def test_qualified_bundle_suppresses_unknown_oi_and_distinguishes_funding_periods(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    end = 30 * 86400
    oi = Observation("fixture", "BTC", "open_interest_amount", end - 7 * 86400,
                     end - 7 * 86400, 100, "unknown", "USD")
    funding = Observation("fixture", "BTC", "funding_rate_actual", end - 7 * 86400,
                          end - 7 * 86400, 0.01, "rate", "USD",
                          {"funding_period_hours": 4, "value_kind": "actual"})
    engine.store.put_observations([oi, replace(oi, event_time=end, observed_at=end, value=200), funding,
                                   replace(funding, event_time=end, observed_at=end,
                                           metadata={"funding_period_hours": 8, "value_kind": "actual"})])
    bundle = engine.bundle(as_of=end, include_forecast=False)
    points = bundle["analysis"]["evidence"]["observations"]
    assert len([point for point in points if point["metric"].startswith("funding")]) == 2
    assert all(point["changes"]["7"] is None for point in points)
    oi_context = next(point for point in bundle["additional_context"]["observations"] if point["metric"].startswith("open_interest"))
    assert oi_context["change"] is None and oi_context["comparable"] is False


def test_evidence_archive_is_lossless_and_detects_corruption():
    original = {"candles": [{"value": value, "source": "synthetic"} for value in range(50000)]}
    archive = archive_evidence(original)
    assert restore_evidence(archive) == original
    assert len(archive["data"]) < archive["uncompressed_bytes"]
    with pytest.raises(ValueError):
        restore_evidence({**archive, "sha256": "0" * 64})


def test_collector_funding_metrics_keep_explicit_actual_and_projected_comparisons():
    from crypto_market_analysis.source_catalog import interpret_context

    actual = Observation("fixture", "BTC", "funding_rate_actual", 100, 100, 0.001,
                         "rate", "USD", {"funding_interval_hours": 8})
    projected = replace(actual, metric="funding_rate_projected")
    rows = [actual, projected, replace(actual, event_time=200, observed_at=200, value=0.002),
            replace(projected, event_time=200, observed_at=200, value=0.003)]
    result = interpret_context(rows, 200)["observations"]
    assert {row["value_kind"] for row in result} == {"actual", "projected"}
    assert all(row["comparable"] for row in result)
    assert sorted(row["change"] for row in result) == [0.001, 0.002]


def test_legacy_evaluation_excludes_preissuance_barrier_ambiguity():
    from crypto_market_analysis.research import evaluate

    day = 86400
    data = [Candle("fixture", "BTC", "USD", "1h", hour * 3600, (hour + 1) * 3600,
                   100, 101, 99, 100, 1, (hour + 1) * 3600 + 120) for hour in range(90 * 24)]
    first_hour = 82 * 24
    data[first_hour] = replace(data[first_hour], low=80)
    ambiguous = evaluate(data, horizon=1, threshold=0.1)
    assert ambiguous["origins"][0]["outcomes"]["down_barrier"] is None
    assert ambiguous["metrics"]["down_barrier"]["ambiguous_origins_excluded"] >= 1
    data[first_hour + 1] = replace(data[first_hour + 1], low=80)
    definite = evaluate(data, horizon=1, threshold=0.1)
    assert definite["origins"][0]["outcomes"]["down_barrier"] == 1


def test_two_year_assessment_with_plan_can_be_issued_without_duplicate_history(tmp_path):
    end = int(time.time()) // 86400 * 86400
    start = end - 731 * 86400
    rows = [Candle("fixture", "BTC", "EUR", "1h", start + i * 3600, start + (i + 1) * 3600,
                   100, 101, 99, 100, 1, start + (i + 1) * 3600, data_mode="demo") for i in range(731 * 24)]
    engine = Engine(Settings(data_dir=tmp_path), "demo")
    engine.store.put_candles(rows)
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture", "data_mode": "demo",
              "start_time": end - 180 * 86400,
              "contributions": [{"time": end - 180 * 86400, "amount": "100"}],
              "schedule": [{"time": end - 180 * 86400, "amount": "90"}]}
    record = engine.issue_assessment("two-year", "BTC", "fixture", "EUR", end, config)
    evidence = restore_evidence(record["evidence"])
    assert len(evidence["candles"]) == 731 * 24
    assert evidence["plan_candle_versions"] == []
    assert record["payload"]["plan"]["plans"]["regular"]["terminals"]["180"]["status"] == "complete"
