import asyncio
import json
import sys
from pathlib import Path

import pytest

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset, RESTCapture
from crypto_market_analysis.models import Candle

DAY = 86400
END = 200 * DAY


@pytest.fixture
def market(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path / "market"), "demo")
    engine.store.put_candles([
        Candle("fixture", "BTC", "EUR", "1h", t, t + 3600, 100, 101, 99, 100, 1,
               t + 3600, data_mode="demo") for t in range(0, END, 3600)
    ])
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "data_mode": "demo", "start_time": END - 180 * DAY,
              "contributions": [{"time": END - 180 * DAY, "amount": "100"}],
              "schedule": [{"time": END - 180 * DAY, "amount": "90"}],
              "fees": {"fixed": "1", "rate": "0", "asset": "quote"}}
    return engine, config


def test_cli_brief_costs_and_price_free_preview(market, tmp_path, capsys):
    engine, config = market
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(config))
    costs = tmp_path / "costs.json"
    costs.write_text(json.dumps({"cases": [{"name": "low", "fees": {"fixed": "0"}},
                                          {"name": "high", "fees": {"fixed": "2"}}]}))
    common = ["--data-dir", str(engine.settings.data_dir), "--mode", "demo", "--as-of", str(END)]
    assert main(["accumulation-brief", *common, "--plan-config", str(plan)]) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["market"]["quote"] == "EUR"
    assert brief["input_reference"] == brief["current_observed"]["context"]["input_reference"]
    assert brief["historical_outcomes"]["180"]["status"] == "complete"
    assert main(["cost-sensitivity", *common, "--plan-config", str(plan), "--cost-cases", str(costs)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cases"][0]["plans"]["regular"]["reserve"] == "10"
    assert result["cases"][1]["plans"]["regular"]["reserve"] == "8"
    assert main(["plan-preview", "--plan-config", str(plan), "--as-of", "0", "--format", "markdown"]) == 0
    report = capsys.readouterr().out
    assert "funding constraints" in report and "Future contributions" in report


def test_empty_journal_summary_does_not_create_journal(market, capsys):
    engine, _ = market
    assert main(["journal-summary", "--data-dir", str(engine.settings.data_dir), "--as-of", str(END)]) == 0
    assert json.loads(capsys.readouterr().out)["assessment_count"] == 0
    with engine.store.connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None


def test_future_plan_issuance_observable_mark_and_original_retry(market, monkeypatch):
    engine, config = market
    issued = END + 1800
    config = {**config, "start_time": END + 3600,
              "contributions": [{"time": END + 3600, "amount": "100"}],
              "schedule": [{"time": END + 3600, "amount": "90"}]}
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: issued)
    original = engine.issue_assessment("future", "BTC", "fixture", "EUR", plan_config=config)
    assert original["payload"]["plan"]["status"] == "not_started"
    assert original["payload"]["evidence_budget"]["capture_profile"] == "candles"
    assert original["payload"]["outcome_protocol"]["endpoint_convention"] == "hourly_after_deadline_v1"
    assert engine.observe_assessment(original["assessment_id"])["payload"]["plan"]["status"] == "not_started"
    now = END + 26 * 3600
    engine.store.put_candles([
        Candle("fixture", "BTC", "EUR", "1h", t, t + 3600, 100, 101, 99, 100, 1,
               t + 3600, data_mode="demo") for t in range(END, now, 3600)
    ])
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: now)
    outcome = engine.observe_assessment(original["assessment_id"])["payload"]
    one_day = [window for window in outcome["windows"] if window["horizon_days"] == 1][0]
    assert one_day["endpoint_below_anchor"]["state"] == "ambiguous"
    assert one_day["observable_endpoint"]["state"] == "known_false"
    assert one_day["observable_endpoint"]["offset_seconds"] == 1800
    assert engine.issue_assessment("future", "BTC", "fixture", "EUR", plan_config=config) == original
    summary = engine.journal_summary()
    assert summary["assessment_count"] == 1
    assert summary["plans"][0]["temporality"] == "unverified"  # Demo never becomes prospective evidence.
    assert summary["counts"]["excluded"] > 0


def test_dataset_lineage_is_visible_to_normal_import_engine(tmp_path):
    config = DatasetConfig("named-lineage", str(tmp_path / "research"), 0, 3600)
    dataset = HistoryDataset(config, clock=lambda: 7200)
    dataset.append_rest("BTC", "EUR", lambda *_: RESTCapture([[0, 100, 101, 99, 100, 1]]), since=0)
    engine = Engine(Settings(data_dir=dataset.directory), "import")
    result = engine.accumulation_brief("BTC", "kraken", "EUR", 7200)
    assert result["input_reference"]["dataset_lineage"]["name"] == "named-lineage"
    assert result["input_reference"]["available_at"] == 7200
    assert engine.context("BTC", "kraken", "USD", 7200)["status"] == "unavailable"


def test_observed_references_describe_new_consumed_data_and_keep_training_separate(market, monkeypatch):
    engine, config = market
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: END)
    original = engine.issue_assessment("outcome-provenance", "BTC", "fixture", "EUR", plan_config=config)
    now = END + 26 * 3600
    engine.store.put_candles([
        Candle("fixture", "BTC", "EUR", "1h", t, t + 3600, 100, 101, 99, 100, 1,
               t + 3600, data_mode="demo") for t in range(END, now, 3600)
    ])
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: now)
    outcome = engine.observe_assessment(original["assessment_id"])["payload"]
    assert outcome["input_reference"]["as_of"] == now
    assert outcome["input_reference"]["available_at"] == now
    assert outcome["original_input_reference"] == original["payload"]["input_reference"]
    assert outcome["plan"]["input_reference"]["available_at"] == now
    assert engine.assessment(original["assessment_id"]) == original


def test_new_mcp_tools_compute_without_persisting(market):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    engine, config = market

    async def check():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "crypto_market_analysis", "serve",
            "--mode", "demo", "--data-dir", str(engine.settings.data_dir)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert all(tool.annotations.readOnlyHint for tool in tools.tools)
            calls = [
                ("get_accumulation_brief", {"asset": "BTC", "source": "fixture", "quote": "EUR", "as_of": END}),
                ("preview_purchase_funding", {"config": config, "as_of": END}),
                ("compare_cost_sensitivity", {"config": config, "cost_cases": [{"name": "declared"}], "as_of": END}),
                ("get_journal_summary", {"as_of": END}),
                ("construct_purchase_plan", {"request": {"asset": "BTC", "quote": "EUR", "source": "fixture",
                    "data_mode": "demo", "start_date": "2026-01-01T00:00:00Z",
                    "recurrence": {"frequency": "weekly", "count": 2},
                    "contribution_amount": "100", "purchase_amount": "90"}}),
            ]
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                assert not result.isError, (name, result)

    asyncio.run(check())
    with engine.store.connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None
