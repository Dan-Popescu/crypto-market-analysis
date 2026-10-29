import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

import pytest

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle

DAY = 86400


@pytest.fixture
def market(tmp_path):
    end = int(time.time()) // 3600 * 3600
    start = end - 200 * DAY
    engine = Engine(Settings(data_dir=tmp_path), "demo")
    rows = [Candle("fixture", "BTC", "EUR", "1h", start + hour * 3600,
                   start + (hour + 1) * 3600, 100, 101, 99, 100, 1,
                   start + (hour + 1) * 3600, data_mode="demo") for hour in range(200 * 24)]
    engine.store.put_candles(rows)
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "data_mode": "demo", "start_time": end - 180 * DAY,
              "contributions": [{"time": end - 180 * DAY, "amount": "100"}],
              "schedule": [{"time": end - 180 * DAY, "amount": "90"}],
              "fees": {"fixed": "1", "rate": "0", "asset": "quote"}}
    return engine, config, end


def test_real_engine_context_windows_and_accounting_share_native_identity(market):
    engine, config, end = market
    context = engine.context("BTC", "fixture", "EUR", end)
    assert context["market"]["quote"] == "EUR"
    assert len(context["price_history"]) >= 199
    assert context["lookbacks"]["730"]["return"]["value"] is None
    windows = engine.purchase_windows("BTC", "fixture", "EUR", end, [1], [0.05])
    assert windows["anchor_price"] == 100
    assert windows["windows"]["1"]["0.05"]["effective_samples"] >= 20
    plans = engine.compare_plans(config, end)
    regular = plans["plans"]["regular"]
    assert regular["reserve"] == "9"
    assert regular["net_quantity"] == "0.9"
    assert regular["terminals"]["180"]["total_wealth"] == "99"
    assert plans["input_reference"]["quote"] == "EUR"
    assert engine.context("BTC", "fixture", "USD", end)["status"] == "unavailable"


def test_actual_issuance_retries_and_outcomes_preserve_original(market):
    engine, config, end = market
    first = engine.issue_assessment("test-local", "BTC", "fixture", "EUR", end, config)
    assert first["issued_at"] >= end
    assert first["payload"]["plan"]["plans"]["regular"]["reserve"] == "9"
    assert engine.issue_assessment("test-local", "BTC", "fixture", "EUR", end, config) == first
    with pytest.raises(ValueError, match="different request"):
        engine.issue_assessment("test-local", "BTC", "fixture", "USD", end, config)
    outcome = engine.observe_assessment(first["assessment_id"])
    assert len(outcome["payload"]["windows"]) == 12
    assert engine.assessment(first["assessment_id"]) == first
    assert len(engine.assessment_outcomes(first["assessment_id"])) == 1


def test_concurrent_engine_issuance_recovers_complete_original_after_capture_race(market, monkeypatch):
    engine, _, end = market
    ready = Barrier(2)
    clock = local()

    class RacingEngine(Engine):
        def capture_inputs(self, *args, **kwargs):
            captured = super().capture_inputs(*args, **kwargs)
            ready.wait(timeout=30)
            return captured

    racing = RacingEngine(engine.settings, "demo")
    racing.dataset_lineage = {"name": "original-market"}
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: clock.now)

    def issue(now):
        clock.now = now
        return racing.issue_assessment("racing-request", "BTC", "fixture", "EUR")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(issue, (end + 1, end + 2)))
    assert results[0] == results[1]
    original = results[0]
    assert original["evidence"]
    assert original["payload"]["input_reference"]["dataset_lineage"] == {"name": "original-market"}
    clock.now = end + DAY
    engine.dataset_lineage = {"name": "changed-market"}
    retry = engine.issue_assessment("racing-request", "BTC", "fixture", "EUR")
    assert retry == original
    assert engine.assessment(original["assessment_id"]) == original
    assert engine.journal_summary()["assessment_count"] == 1


def test_readonly_calculations_do_not_create_journal_or_watch_tables(market):
    engine, _, end = market
    engine.context("BTC", "fixture", "EUR", end)
    result = engine.vigilance("BTC", "fixture", "EUR", end)
    assert result["quality"] == "degraded"
    with engine.store.connection() as con:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "assessment_journal" not in names
    assert "vigilance_states" not in names


def test_real_cli_reports_and_plan_chart(tmp_path, capsys, market):
    engine, config, end = market
    file = tmp_path / "plan.json"
    file.write_text(json.dumps(config))
    common = ["--data-dir", str(engine.settings.data_dir), "--as-of", str(end), "--mode", "demo"]
    assert main(["compare-plans", *common, "--plan-config", str(file)]) == 0
    assert json.loads(capsys.readouterr().out)["plans"]["regular"]["reserve"] == "9"
    assert main(["context", *common, "--source", "fixture", "--quote", "EUR", "--format", "markdown"]) == 0
    report = capsys.readouterr().out
    assert "EUR" in report and "fixture" in report
    output = tmp_path / "plan.svg"
    assert main(["chart", *common, "--kind", "plans", "--plan-config", str(file), "--output", str(output)]) == 0
    capsys.readouterr()
    svg = output.read_text()
    assert "reserve" in svg.lower() and "EUR" in svg


def test_calendar_import_is_idempotent_and_included_in_captured_evidence(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    calendar = tmp_path / "cpi.ics"
    calendar.write_text("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:synthetic-cpi\n"
                        "SUMMARY:Consumer Price Index\nDTSTART;TZID=America/New_York:20260312T083000\n"
                        "END:VEVENT\nEND:VCALENDAR\n")
    assert engine.import_calendar(calendar)["inserted"] == 1
    assert engine.import_calendar(calendar)["inserted"] == 0
    bundle = engine.bundle(include_forecast=False)
    assert len(bundle["calendar"]) == 1
    assert bundle["calendar"][0]["publication_time_verified"] is False


def test_actual_mcp_accumulation_tools_compute_without_persisting(market):
    import asyncio
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    engine, config, end = market

    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--mode", "demo", "--data-dir", str(engine.settings.data_dir)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert all(tool.annotations.readOnlyHint for tool in listed.tools)
            arguments = {"asset": "BTC", "source": "fixture", "quote": "EUR", "as_of": end}
            context = await session.call_tool("get_market_context", arguments)
            assert not context.isError
            assert context.structuredContent["market"]["quote"] == "EUR"
            windows = await session.call_tool("get_purchase_windows", {**arguments, "horizons": [1], "thresholds": [0.05]})
            assert not windows.isError
            comparison = await session.call_tool("compare_purchase_plans", {"config": config, "as_of": end})
            assert not comparison.isError
            assert comparison.structuredContent["plans"]["regular"]["reserve"] == "9"
            evaluation = await session.call_tool("evaluate_purchase_plans", {"config": config, "as_of": end})
            assert not evaluation.isError
            vigilance = await session.call_tool("get_vigilance", arguments)
            assert not vigilance.isError
            assert vigilance.structuredContent["quality"] == "degraded"
            missing = await session.call_tool("get_assessment", {"assessment_id": "missing"})
            assert missing.isError

    asyncio.run(check())
    with engine.store.connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None


def test_mcp_plan_financial_summaries_survive_bounded_detail_paging(market):
    """Large trajectories must not displace plan values, terminals or unknowns."""
    import asyncio
    import hashlib

    from mcp.shared.memory import create_connected_server_and_client_session

    from crypto_market_analysis.mcp_results import RESPONSE_BYTE_LIMIT, canonical_json
    from crypto_market_analysis.server import create_server

    engine, config, end = market
    expected = engine.compare_plans(config, end)
    server = create_server(engine.settings, mode="demo")

    def assert_scalar_leaves(actual, original):
        for key, value in original.items():
            if isinstance(value, dict):
                assert_scalar_leaves(actual[key], value)
            elif not isinstance(value, list):
                assert actual[key] == value

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("compare_purchase_plans", {"config": config, "as_of": end})
            assert not result.isError
            summary = result.structuredContent
            assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) < RESPONSE_BYTE_LIMIT
            for family, original in expected["plans"].items():
                # This catches removal of the plan map and any financial/status/
                # uncertainty submap even when the response is otherwise valid.
                assert_scalar_leaves(summary["plans"][family], original)
                assert summary["plans"][family]["trajectory"]["_omitted"] is True
            metadata = summary["_mcp_result"]
            assert "/plans" not in metadata["omitted_paths"]
            assert metadata["content_hash"] == hashlib.sha256(canonical_json(expected)).hexdigest()
            for family, original in expected["plans"].items():
                path = f"/plans/{family}/trajectory"
                assert path in metadata["omitted_paths"]
                binding = {key: metadata[key] for key in ("handle", "selection_hash", "content_hash")}
                restored, offset = [], 0
                while offset is not None:
                    response = await session.call_tool("get_result_page", {**binding, "path": path,
                                                                           "offset": offset, "limit": 100})
                    assert not response.isError
                    assert len(response.model_dump_json(by_alias=True, exclude_none=True).encode()) < RESPONSE_BYTE_LIMIT
                    page = response.structuredContent
                    restored.extend(page["items"])
                    offset = page["next_offset"]
                assert restored == original["trajectory"]

    asyncio.run(check())
