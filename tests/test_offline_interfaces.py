"""Exercise real offline CLI files and one read-only MCP transport session."""

import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle
from crypto_market_analysis.reporting import bounded_as_of

ROOT = Path(__file__).resolve().parents[1]
NOW = 1793145660


def example(name):
    return json.loads((ROOT / "examples" / name).read_text())


@pytest.fixture
def issued(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=tmp_path / "market"), "demo")
    end = NOW - NOW % 3600
    engine.store.put_candles([
        Candle("fixture", "BTC", "EUR", "1h", start, start + 3600, 100, 101, 99, 100, 1,
               start + 3600, data_mode="demo") for start in range(end - 48 * 3600, end, 3600)
    ])
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: NOW)
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "data_mode": "demo", "start_time": NOW + 3600,
              "contributions": [{"time": NOW + 3600, "amount": "100"}],
              "schedule": [{"time": NOW + 3600, "amount": "90"}]}
    record = engine.issue_assessment("offline-interfaces", "BTC", "fixture", "EUR", plan_config=config)
    return engine, record


@pytest.mark.parametrize(("command", "fixture", "as_of", "expected"), [
    ("budget-preview", "shared-budget.json", 1767225600, "Shared cash envelope"),
    ("purchase-costs", "offline-purchase-costs.json", 1793145600, "Received base units"),
    ("venue-disagreement", "offline-venue-disagreement.json", 1793145600, "Median deviation"),
    ("quote-context", "quote-context.json", NOW, "Basis (%)"),
    ("derivatives-context", "derivatives-context.json", 1767398400, "Native change"),
])
def test_request_cli_computes_json_and_meaningful_markdown_without_store(
    tmp_path, monkeypatch, command, fixture, as_of, expected,
):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(example(fixture)))

    def forbidden(*args, **kwargs):
        raise AssertionError("Pure request calculations must not initialize an Engine or Store")

    monkeypatch.setattr("crypto_market_analysis.cli.Engine", forbidden)
    output = tmp_path / "report.json"
    args = [command, "--request", str(request), "--as-of", str(as_of), "--output", str(output),
            "--data-dir", str(tmp_path / "unused")]
    assert main(args) == 0
    result = json.loads(output.read_text())
    assert result["as_of"] == as_of
    assert result["data_mode"] == "demo"
    assert not (tmp_path / "unused").exists()
    assert main([*args, "--format", "markdown"]) == 0
    report = output.read_text()
    assert expected in report
    assert "SYNTHETIC DEMONSTRATION" in report
    assert '"snapshots"' not in report


def test_journal_cli_scorecard_and_replay_preserve_original_and_outcomes(issued, tmp_path):
    engine, original = issued
    output = tmp_path / "result.json"
    common = ["--data-dir", str(engine.settings.data_dir), "--mode", "demo", "--output", str(output)]
    assert main(["journal-scorecard", *common, "--as-of", str(NOW), "--limit", "1"]) == 0
    scorecard = json.loads(output.read_text())
    assert scorecard["assessment_count"] == 1
    assert scorecard["selection"]["limit"] == 1
    assert main(["replay-assessment", *common, "--id", original["assessment_id"]]) == 0
    replay = json.loads(output.read_text())
    assert replay["status"] == "verified"
    assert replay["context"] == original["payload"]["context"]
    assert main(["replay-assessment", *common, "--id", original["assessment_id"], "--format", "markdown"]) == 0
    assert "Original calculations verified" in output.read_text()
    assert main(["journal-scorecard", *common, "--as-of", str(NOW), "--format", "markdown"]) == 0
    assert "Rate denominator" in output.read_text()
    assert "Plan cohort" in output.read_text()
    assert "Brier score and calibration: unavailable" in output.read_text()
    assert engine.assessment(original["assessment_id"]) == original
    assert engine.assessment_outcomes(original["assessment_id"]) == []


def test_recovery_cli_preinitialization_and_full_roundtrip(issued, tmp_path, capsys):
    engine, original = issued
    missing = tmp_path / "missing.sqlite3"
    unrelated = tmp_path / "unused"
    bundle = tmp_path / "bundle"
    assert main(["backup", "--database", str(missing), "--destination", str(bundle),
                 "--data-dir", str(unrelated)]) == 2
    assert not missing.exists() and not bundle.exists() and not unrelated.exists()
    capsys.readouterr()
    assert main(["backup", "--database", str(engine.settings.database), "--destination", str(bundle),
                 "--output", str(engine.settings.database)]) == 2
    assert not bundle.exists()
    capsys.readouterr()
    parameters = [sys.executable, "-m", "crypto_market_analysis"]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    output = subprocess.run([*parameters, "backup", "--database", str(engine.settings.database),
                             "--destination", str(bundle), "--data-dir", str(unrelated)],
                            capture_output=True, text=True, env=env, check=True)
    assert json.loads(output.stdout)["status"] == "verified"
    assert not unrelated.exists()
    assert main(["verify-backup", "--bundle", str(bundle), "--data-dir", str(unrelated)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    before = (bundle / engine.settings.database.name).read_bytes()
    assert main(["verify-backup", "--bundle", str(bundle), "--output", str(bundle / engine.settings.database.name)]) == 2
    assert (bundle / engine.settings.database.name).read_bytes() == before
    capsys.readouterr()
    restored = tmp_path / "restored"
    assert main(["restore-backup", "--bundle", str(bundle), "--destination", str(restored),
                 "--data-dir", str(unrelated)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "restored"
    assert Engine(Settings(data_dir=restored), "demo").assessment(original["assessment_id"]) == original
    assert not unrelated.exists()


def test_explicit_ecb_manual_import_cli(tmp_path, capsys):
    assert main(["import-calendar", "--kind", "ecb-manual", "--file",
                 str(ROOT / "examples" / "ecb-manual-calendar.json"), "--data-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["inserted"] > 0
    assert all(row["origin"] == "sourced_manual_schedule" for row in result["events"])


@pytest.mark.parametrize("value", [True, "123", 1.5, -1])
def test_cutoff_validation_rejects_coercion(value):
    with pytest.raises(ValueError, match="as_of"):
        bounded_as_of(value)


def test_new_tools_compute_over_one_stdio_session_without_mutation(issued):
    engine, original = issued
    calls = [
        ("preview_shared_budget", {"request": example("shared-budget.json"), "as_of": 1767225600}),
        ("calculate_purchase_costs", {"request": example("offline-purchase-costs.json"), "as_of": 1793145600}),
        ("calculate_venue_disagreement", {"request": example("offline-venue-disagreement.json"), "as_of": 1793145600}),
        ("get_quote_context", {"request": example("quote-context.json"), "as_of": NOW}),
        ("get_derivatives_context", {"request": example("derivatives-context.json"), "as_of": 1767398400}),
        ("get_journal_scorecard", {"as_of": NOW, "limit": 1}),
        ("replay_assessment", {"assessment_id": original["assessment_id"]}),
    ]

    async def check():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--mode", "demo", "--data-dir", str(engine.settings.data_dir)],
            env={"PYTHONPATH": str(ROOT / "src")})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert not {"backup", "verify_backup", "restore_backup"} & tools.keys()
            for name, arguments in calls:
                tool = tools[name]
                assert tool.annotations.readOnlyHint and not tool.annotations.openWorldHint
                assert not {"path", "file", "database", "destination", "bundle"} & tool.inputSchema["properties"].keys()
                result = await session.call_tool(name, arguments)
                assert not result.isError, (name, result)
                assert result.structuredContent is not None
                data = result.structuredContent
                if name == "preview_shared_budget":
                    assert data["current"]["locked_cash"] == "75"
                    assert data["current"]["reserved_immediate_cash"] == "25"
                    assert data["current"]["free_cash"] == "0"
                elif name == "calculate_purchase_costs":
                    assert data["rows"][0]["hypothetical_fill"]["quantity_received"] == "1"
                    assert data["rows"][0]["hypothetical_fill"]["all_in_cost_quote"] == "112.1"
                    assert data["rows"][1]["status"] == "missing"
                elif name == "calculate_venue_disagreement":
                    assert data["range"]["range_quote"] == "20"
                    assert data["rows"][2]["outlier"] is True
                    assert data["rows"][3]["status"] == "unsupported"
                elif name == "get_quote_context":
                    assert Decimal(data["stablecoin_basis"][0]["basis_percent"]) == Decimal("-1")
                    assert Decimal(data["stablecoin_basis"][1]["basis_percent"]) == Decimal("0.1")
                elif name == "get_derivatives_context":
                    series = {row["identity"]["metric"]: row for row in data["series"]}
                    assert series["open_interest_amount"]["comparisons"]["4h"]["native_change"] == 25000
                    assert series["open_interest_amount"]["comparisons"]["24h"]["native_change"] == 35000
                    assert series["funding_rate_projected"]["comparisons"]["4h"]["native_change"] == pytest.approx(0.0001)
                    assert series["funding_rate_projected"]["identity"]["dimension"]["quote"] == "USDT"
                elif name == "get_journal_scorecard":
                    assert data["assessment_count"] == 1
                    assert data["plan_cohorts"][0]["counts"]["90"]["pending"] == 1
                elif name == "replay_assessment":
                    assert data["status"] == "verified"
                    assert data["plan"]["status"] == "not_started"
            for invalid in (True, "123"):
                result = await session.call_tool("get_quote_context", {"request": example("quote-context.json"), "as_of": invalid})
                assert result.isError
            nested = {}
            for _ in range(25):
                nested = {"nested": nested}
            result = await session.call_tool("get_quote_context", {"request": nested})
            assert result.isError
            result = await session.call_tool("get_journal_scorecard", {"limit": 101, "as_of": NOW})
            assert result.isError
            result = await session.call_tool("replay_assessment", {"assessment_id": ""})
            assert result.isError

    asyncio.run(check())
    assert engine.assessment(original["assessment_id"]) == original
    assert engine.assessment_outcomes(original["assessment_id"]) == []
