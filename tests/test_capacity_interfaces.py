import asyncio
import json
import sys
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.models import Candle
from crypto_market_analysis.request_schemas import get_request_schema
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store

NOW = 10 * 86400
TOOLS = {"get_http_accounting", "get_auto_repair_status", "get_analysis_dossier", "get_installation_qualification"}


def test_new_cli_inspections_and_stdin_do_not_initialize_missing_data(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "absent"
    for command in ("http-accounting", "auto-repair-history", "qualify-installation"):
        assert main([command, "--data-dir", str(directory), "--mode", "import"]) in (0, 1)
        assert json.loads(capsys.readouterr().out)
        assert not directory.exists()
        assert main([command, "--data-dir", str(directory), "--mode", "import", "--format", "markdown"]) in (0, 1)
        assert capsys.readouterr().out.startswith("# ")
        assert not directory.exists()
    monkeypatch.setattr(sys, "stdin", StringIO('{"sources":["synthetic"],"data_mode":"demo"}'))
    assert main(["qualify-installation", "--request", "-", "--data-dir", str(directory)]) in (0, 1)
    assert json.loads(capsys.readouterr().out)["data_mode"] == "demo"
    assert not directory.exists()


def test_new_cli_rejects_invalid_write_clock_and_protected_output_before_initializing(tmp_path, capsys):
    directory = tmp_path / "absent"
    assert main(["auto-repair-history", "--data-dir", str(directory), "--mode", "import",
                 "--apply", "--as-of", str(NOW)]) == 2
    assert "omit --as-of" in capsys.readouterr().err
    assert main(["qualify-installation", "--data-dir", str(directory), "--refresh", "--as-of", str(NOW)]) == 2
    assert "Refresh requires" in capsys.readouterr().err
    assert main(["qualify-installation", "--data-dir", str(directory), "--output", str(directory / "observer.sqlite")]) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert not directory.exists()


def test_stdin_request_limit_is_in_bytes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", StringIO('{"x":"' + "é" * 1_000_000 + '"}'))
    assert main(["qualify-installation", "--data-dir", str(tmp_path / "absent"), "--request", "-"]) == 2
    assert "2 MB" in capsys.readouterr().err
    assert not (tmp_path / "absent").exists()


def test_registered_capacity_contract_examples_execute_on_existing_store(tmp_path):
    Store(tmp_path / "observer.sqlite")
    server = create_server(Settings(data_dir=tmp_path), "demo")

    async def check():
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert TOOLS <= tools.keys()
        assert all(tools[name].annotations.readOnlyHint for name in TOOLS)
        for name in ("get_analysis_dossier", "get_installation_qualification"):
            contract = get_request_schema(name)
            for example in contract["examples"]:
                jsonschema.validate(example, contract["input_schema"])
                response = await server.call_tool(name, example)
                assert not response.isError
                assert len(response.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
        with pytest.raises(ToolError, match="read-only"):
            await server.call_tool("get_installation_qualification", {"request": {"refresh": True}})

    asyncio.run(check())


def test_actual_stdio_capacity_reads_keep_missing_store_absent(tmp_path):
    directory = tmp_path / "absent"

    async def check():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--mode", "import", "--data-dir", str(directory)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert TOOLS <= tools.keys()
            assert all(tool.annotations.readOnlyHint for tool in tools.values())
            for name, arguments in (("get_http_accounting", {}), ("get_auto_repair_status", {}),
                                    ("get_installation_qualification", {"request": {}})):
                response = await session.call_tool(name, arguments)
                assert not response.isError
            response = await session.call_tool("get_installation_qualification", {"request": {"refresh": True}})
            assert response.isError
    asyncio.run(check())
    assert not directory.exists()


def test_multi_market_dossier_cli_and_mcp_keep_native_selections(tmp_path, capsys):
    store = Store(tmp_path / "observer.sqlite")
    markets = [{"asset": asset, "source": "synthetic", "quote": quote, "lookbacks": [7]}
               for asset in ("BTC", "ETH") for quote in ("EUR", "USD")]
    store.put_candles([Candle(m["source"], m["asset"], m["quote"], "1h", t, t + 3600,
                             price, price, price, price, 1, t + 3600, data_mode="demo")
                      for m, price in zip(markets, (100, 200, 10, 20), strict=True)
                      for t in range(NOW - 8 * 86400, NOW, 3600)])
    request = {"data_mode": "demo", "markets": markets}
    file = tmp_path / "request.json"
    file.write_text(json.dumps(request))
    assert main(["analysis-dossier", "--data-dir", str(tmp_path), "--request", str(file), "--as-of", str(NOW)]) == 0
    full = json.loads(capsys.readouterr().out)
    assert len(full["markets"]) == 4
    assert len({(row["selection"]["asset"], row["selection"]["quote"]) for row in full["markets"]}) == 4
    assert all(row["market_context"]["stored_inputs"]["watermark"] == full["watermark"] for row in full["markets"])
    assert main(["analysis-dossier", "--data-dir", str(tmp_path), "--request", str(file),
                 "--as-of", str(NOW), "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert "BTC/EUR" in markdown and "ETH/USD" in markdown
    assert "consumed_records" not in markdown

    async def check():
        response = await create_server(Settings(data_dir=tmp_path), "demo").call_tool(
            "get_analysis_dossier", {"request": request, "as_of": NOW})
        assert not response.isError
        assert isinstance(response.structuredContent["markets"], list)
        assert len(response.structuredContent["markets"]) == 4
        assert len(response.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
    asyncio.run(check())


def test_auto_repair_cli_applies_only_explicit_bounded_import_work(tmp_path, monkeypatch, capsys):
    from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset, RESTCapture

    now = int(time.time()) // 3600 * 3600
    dataset = HistoryDataset(DatasetConfig("synthetic", str(tmp_path), now - 7200, now))
    calls = []

    class Fetch:
        client = SimpleNamespace(session=SimpleNamespace(close=lambda: None))

        def __call__(self, asset, quote, since, limit):
            calls.append((asset, quote, since, limit))
            return RESTCapture([[t * 1000, 100, 100, 100, 100, 1]
                                for t in range(since, now, 3600)][:limit])

    monkeypatch.setattr("crypto_market_analysis.history_dataset.KrakenRESTCapture", Fetch)
    file = tmp_path / "request.json"
    file.write_text(json.dumps({"markets": [["BTC", "EUR"]], "max_hours": 2, "request_budget": 1}))
    args = ["auto-repair-history", "--data-dir", str(tmp_path), "--mode", "import", "--request", str(file)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["hours_planned"] == 2
    assert not calls
    assert main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["requests_attempted"] == 1
    assert len(calls) == 1
    assert len(dataset.store.candles("BTC", "kraken", "EUR", now + 3600, "import")) == 2
    assert dataset.store.candles("BTC", "kraken", "EUR", now + 3600, "live") == []
    assert main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["requests_attempted"] == 0
    assert len(calls) == 1
