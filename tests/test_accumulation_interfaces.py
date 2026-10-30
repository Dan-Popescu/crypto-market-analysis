"""Public accumulation adapters keep calculations in the shared engine."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from crypto_market_analysis import cli, server
from crypto_market_analysis.charts import _complete_daily, render_result_chart
from crypto_market_analysis.config import Settings
from crypto_market_analysis.models import Candle
from crypto_market_analysis.reporting import bounded_object, render_accumulation_report


class RecordingEngine:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"status": "unavailable", "as_of": kwargs.get("as_of", 0)}

        return call


@pytest.fixture
def recorded(monkeypatch):
    RecordingEngine.calls = []
    monkeypatch.setattr(cli, "Engine", RecordingEngine)
    monkeypatch.setattr(server, "Engine", RecordingEngine)
    return RecordingEngine.calls


def test_cli_context_window_argument_wiring_and_epoch_zero(recorded, capsys):
    assert (
        cli.main(
            [
                "context",
                "--asset",
                "ETH",
                "--source",
                "kraken",
                "--quote",
                "EUR",
                "--as-of",
                "0",
                "--lookbacks",
                "7,730",
            ]
        )
        == 0
    )
    assert recorded[-1] == ("context", ("ETH", "kraken", "EUR", 0, [7, 730]), {})
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    assert cli.main(["windows", "--as-of", "0", "--horizons", "1,14", "--thresholds", "0.02,0.1"]) == 0
    assert recorded[-1] == ("purchase_windows", ("BTC", None, None, 0, [1, 14], [0.02, 0.1]), {})
    capsys.readouterr()
    before = len(recorded)
    assert cli.main(["windows", "--thresholds", "NaN"]) == 2
    assert "finite fractions" in capsys.readouterr().err
    assert len(recorded) == before


def test_cli_plan_json_bounds_and_evaluation_wiring(recorded, tmp_path, capsys):
    config = tmp_path / "plan.json"
    config.write_text('{"amount":"100"}')
    assert cli.main(["compare-plans", "--plan-config", str(config), "--as-of", "0"]) == 0
    assert recorded[-1] == (
        "compare_plans",
        ({"amount": "100"},),
        {"as_of": 0, "validation": "retrospective"},
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "evaluate-plans",
                "--plan-config",
                str(config),
                "--origins",
                "0,86400",
                "--held-out-start",
                "0",
                "--policy-frozen-at",
                "0",
            ]
        )
        == 0
    )
    assert recorded[-1][2] == {
        "as_of": None,
        "origins": [0, 86400],
        "validation": "known_at",
        "held_out_start": 0,
        "policy_frozen_at": 0,
    }
    capsys.readouterr()
    for contents in ("[]", '{"bad":NaN}', '{"large":"' + "x" * 2_000_000 + '"}'):
        config.write_text(contents)
        assert cli.main(["compare-plans", "--plan-config", str(config)]) == 2
        assert "error" in json.loads(capsys.readouterr().err)


def test_cli_vigilance_and_journal_writes_are_explicit(recorded, capsys):
    assert cli.main(["vigilance", "--as-of", "0"]) == 0
    assert recorded[-1][0] == "vigilance"
    capsys.readouterr()
    assert cli.main(["vigilance", "--state-key", "local-watch", "--horizon-hours", "8"]) == 0
    assert recorded[-1][0:2] == ("record_vigilance", ("local-watch",))
    assert recorded[-1][2]["horizon_hours"] == 8
    capsys.readouterr()
    assert cli.main(["issue", "--idempotency-key", "retry-1", "--as-of", "0"]) == 0
    assert recorded[-1][0:2] == ("issue_assessment", ("retry-1",))
    capsys.readouterr()
    assert cli.main(["observe", "--id", "issued-1", "--as-of", "0"]) == 0
    assert recorded[-1] == ("observe_assessment", ("issued-1",), {"as_of": 0})


def test_mcp_public_tools_wiring_and_bounded_inputs(recorded, tmp_path):
    async def check():
        app = server.create_server(Settings(data_dir=tmp_path))
        tools = await app.list_tools()
        names = {tool.name for tool in tools}
        assert {
            "get_market_context",
            "get_purchase_windows",
            "compare_purchase_plans",
            "evaluate_purchase_plans",
            "get_assessment",
            "get_vigilance",
        } <= names
        assert all(tool.annotations.readOnlyHint for tool in tools)
        assert not {"issue_assessment", "observe_assessment", "record_vigilance"} & names
        await app.call_tool(
            "get_market_context", {"asset": "ETH", "quote": "EUR", "as_of": 0, "lookbacks": [730]}
        )
        assert recorded[-1] == ("context", ("ETH", None, "EUR", 0, [730]), {})
        await app.call_tool("evaluate_purchase_plans", {"config": {}, "origins": [0], "held_out_start": 0})
        assert recorded[-1][0] == "evaluate_plans"
        assert recorded[-1][2]["origins"] == [0]
        before = len(recorded)
        with pytest.raises(ToolError, match="between 1 and 16"):
            await app.call_tool("get_purchase_windows", {"horizons": [1] * 17})
        with pytest.raises(ToolError, match="2000"):
            await app.call_tool("compare_purchase_plans", {"config": {"large": [1] * 2001}})
        assert len(recorded) == before

    asyncio.run(check())


def test_mcp_subprocess_advertises_accumulation_read_only(tmp_path):
    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--data-dir", str(tmp_path)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert all(tool.annotations.readOnlyHint for tool in tools)
            assert "get_market_context" in {tool.name for tool in tools}
            invalid = await session.call_tool("get_purchase_windows", {"thresholds": [0]})
            assert invalid.isError
            assert "strictly between zero and one" in invalid.content[0].text

    asyncio.run(check())


def test_context_chart_draws_actual_730_day_series_and_labels(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    calls = []
    original = Axes.plot

    def capture(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "plot", capture)
    result = {
        "as_of": 730 * 86400,
        "market": {"asset": "BTC", "quote": "EUR", "source": "kraken", "data_mode": "demo"},
        "input_reference": "fixture-730",
        "price_history": [{"time": day * 86400, "close": 100 + day} for day in range(731)],
        "lookbacks": {
            "730": {"lookback_days": 730, "return": {"value": 7.3}, "decline_from_high": {"value": -0.1}}
        },
    }
    output = render_result_chart("context", result, tmp_path / "context.svg")
    assert list(calls[0][0][1]) == list(range(100, 831))
    svg = output.read_text()
    assert all(
        word in svg
        for word in (
            "BTC/EUR",
            "kraken",
            "SYNTHETIC DEMONSTRATION",
            "fixture-730",
            "730",
            "complete UTC days",
        )
    )


def test_purchase_chart_threshold_values_no_future_path(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    values = []
    original = Axes.axhline

    def capture(self, y=0, *args, **kwargs):
        values.append(y)
        return original(self, y, *args, **kwargs)

    monkeypatch.setattr(Axes, "axhline", capture)
    result = {
        "as_of": 0,
        "anchor_price": 100,
        "market": {"quote": "USD"},
        "windows": {"7": {"0.05": {"status": "insufficient", "effective_samples": 3}}},
    }
    output = render_result_chart("windows", result, tmp_path / "windows.svg")
    assert values == [100, 95]
    assert "no future price path" in output.read_text()
    assert "n=3" in output.read_text()


def test_plan_chart_draws_wealth_reserve_and_purchases(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    values = []
    original = Axes.plot

    def capture(self, *args, **kwargs):
        values.append((kwargs.get("label"), args[1]))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "plot", capture)
    result = {
        "as_of": 86400,
        "quote": "EUR",
        "plans": {
            "regular": {
                "trajectory": [
                    {"time": 0, "total_wealth": "110", "reserve": "10"},
                    {"time": 86400, "total_wealth": "120", "reserve": "20"},
                ],
                "history": [{"time": 0, "price": "100"}],
            }
        },
    }
    output = render_result_chart("plans", result, tmp_path / "plans.svg")
    assert values[0] == ("regular: holdings plus reserve", [110.0, 120.0])
    assert values[1] == ("regular: reserve", [10.0, 20.0])
    assert "hypothetical purchases" in output.read_text()


def test_complete_daily_supports_explicit_730_days():
    rows = [
        Candle("fixture", "BTC", "EUR", "1h", t, t + 3600, 100, 101, 99, 100, 1, t + 3600)
        for t in range(0, 730 * 86400, 3600)
    ]
    _, complete = _complete_daily(rows, 730 * 86400, lookback_days=730)
    assert len(complete) == 730
    assert len(_complete_daily(rows, 730 * 86400)[1]) == 365


def test_report_preserves_limits_contradictions_and_determinism():
    result = {
        "as_of": 0,
        "market": {"asset": "BTC", "quote": "EUR", "data_mode": "demo"},
        "contradictions": ["Funding and price disagree."],
        "limitations": ["Source missing."],
        "lookbacks": {"730": {"return": {"value": None}}},
    }
    report = render_accumulation_report(result, "context")
    assert report == render_accumulation_report(result, "context")
    assert all(
        text in report
        for text in ("SYNTHETIC", "BTC/EUR", "unknown", "Funding and price disagree.", "Source missing.")
    )
    with pytest.raises(ValueError, match="finite"):
        bounded_object({"invalid": float("inf")})
