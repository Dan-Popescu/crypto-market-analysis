"""Actual finite CLI and MCP contracts for the five analytical workflows."""

import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread

import jsonschema
import pytest
import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.models import Candle
from crypto_market_analysis.qualification import qualify_sources
from crypto_market_analysis.request_schemas import get_request_schema
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store

NOW = 1704067200
NEW_TOOLS = {"get_analysis_changes", "get_analysis_readiness", "get_plan_robustness", "get_history_backfill_status"}


def dossier():
    return {"data_mode": "demo", "markets": [
        {"asset": asset, "source": "synthetic", "quote": quote, "lookbacks": [7, 30]}
        for asset in ("BTC", "ETH") for quote in ("EUR", "USD")]}


@pytest.mark.parametrize("command", ["analysis-changes", "analysis-readiness", "backfill-history", "plan-robustness"])
def test_new_help_has_examples_and_formats(command, capsys):
    with pytest.raises(SystemExit) as exited:
        main([command, "--help"])
    assert exited.value.code == 0
    text = capsys.readouterr().out
    assert "Example:" in text and "--request" in text and "--format" in text


def test_missing_inspection_and_stdin_do_not_create_store(tmp_path, monkeypatch, capsys):
    absent = tmp_path / "absent"
    for format in ("json", "markdown"):
        monkeypatch.setattr(sys, "stdin", StringIO(json.dumps({"dossier": dossier()})))
        assert main(["analysis-readiness", "--request", "-", "--data-dir", str(absent),
                     "--as-of", str(NOW), "--format", format]) == 0
        text = capsys.readouterr().out
        assert (json.loads(text)["status"] == "missing") if format == "json" else text.startswith("# ")
        assert main(["backfill-history", "--status", "--mode", "import", "--data-dir", str(absent),
                     "--format", format]) == 0
        text = capsys.readouterr().out
        assert (json.loads(text)["status"] == "missing") if format == "json" else text.startswith("# ")
        assert not absent.exists()
    request = {"source": "coinbase", "markets": [{"asset": "BTC", "quote": "EUR"}],
               "start": NOW - 86400, "end": NOW}
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request)))
    assert main(["backfill-history", "--mode", "import", "--request", "-", "--data-dir", str(absent)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert not absent.exists()
    assert main(["backfill-history", "--apply", "--status", "--mode", "import", "--data-dir", str(absent)]) == 2
    assert "read-only" in capsys.readouterr().err and not absent.exists()


def test_all_registered_schema_examples_and_readonly_annotations(tmp_path):
    Store(tmp_path / "observer.sqlite")
    server = create_server(Settings(data_dir=tmp_path), "demo")

    async def check():
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert len(tools) == 64 and NEW_TOOLS <= tools.keys()
        assert all(tool.annotations.readOnlyHint for tool in tools.values())
        for name in NEW_TOOLS - {"get_history_backfill_status"}:
            contract = get_request_schema(name)
            for example in contract["examples"]:
                jsonschema.validate(example, contract["input_schema"])
                result = await server.call_tool(name, example)
                assert not result.isError, (name, result)
                assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
    asyncio.run(check())


def test_four_native_cli_and_mcp_summaries_keep_claim_statuses_and_changes(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "observer.sqlite")
    selection = dossier()
    store.put_candles([Candle("synthetic", market["asset"], market["quote"], "1h", hour, hour + 3600,
                             price, price, price, price, 1, hour + 3600, data_mode="demo")
                      for market, price in zip(selection["markets"], (100, 200, 1000, 2000), strict=True)
                      for hour in range(NOW - 35 * 86400, NOW, 3600)])
    changes = {"dossier": selection, "previous_as_of": NOW - 3600}
    readiness = {"dossier": selection, "qualification": {"required_history_days": 7},
                 "windows": {"horizons": [1], "thresholds": [0.05]}}
    originals = {}
    for command, request in (("analysis-changes", changes), ("analysis-readiness", readiness)):
        monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request)))
        assert main([command, "--request", "-", "--data-dir", str(tmp_path), "--as-of", str(NOW)]) == 0
        result = json.loads(capsys.readouterr().out)
        originals[command] = result
        assert len(result["markets"]) == 4
        if command == "analysis-readiness":
            assert all(row["claim_statuses"] == {claim["claim"]: claim["status"] for claim in row["claims"]}
                       for row in result["markets"])

    async def check():
        server = create_server(Settings(data_dir=tmp_path), "demo")
        for name, request in (("get_analysis_changes", changes), ("get_analysis_readiness", readiness)):
            result = await server.call_tool(name, {"request": request, "as_of": NOW})
            assert not result.isError
            assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
            view = result.structuredContent
            assert isinstance(view["markets"], list) and len(view["markets"]) == 4
            assert len({(row["selection"]["asset"], row["selection"]["quote"]) for row in view["markets"]}) == 4
            if name == "get_analysis_readiness":
                original = originals["analysis-readiness"]
                assert [row["claim_statuses"] for row in view["markets"]] == [row["claim_statuses"] for row in original["markets"]]
                assert view["next_actions"] and all(action["execute"] is False for action in view["next_actions"])
                metadata = view["_mcp_result"]
                page = await server.call_tool("get_result_page", {key: metadata[key] for key in
                                              ("handle", "selection_hash", "content_hash")} |
                                              {"path": "/markets/0/claims", "limit": 1})
                assert not page.isError
                assert page.structuredContent["items"][0] == original["markets"][0]["claims"][0]
    asyncio.run(check())


def test_readiness_work_budget_rejected_before_store_open(tmp_path):
    from crypto_market_analysis.analysis_readiness import build_analysis_readiness

    request = {"dossier": dossier(), "windows": {"horizons": [1, 3, 7, 14],
               "thresholds": [0.01, 0.02, 0.03, 0.04, 0.05]}}
    absent = tmp_path / "absent" / "observer.sqlite"
    with pytest.raises(ValueError, match="64"):
        build_analysis_readiness(Store(absent, initialize=False), request, NOW)
    assert not absent.parent.exists()


def test_backfill_provenance_bridge_rejects_injected_empty_and_other_native_markets(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    record = {"record_id": "synthetic-backfill", "source": "coinbase", "source_url": "https://example.invalid/fixture",
              "asset": "BTC", "quote": "EUR", "data_mode": "import", "observed_at": NOW,
              "captured_at": NOW, "validated_rows": 1, "capture_evidence": "injected_clients_unqualified",
              "response_sha256": "0" * 64, "raw_evidence_retained": False}

    def access(source="coinbase", market="BTC/EUR", cutoff=NOW):
        return qualify_sources(store, {"sources": [source], "markets": [market], "data_mode": "import",
                                      "required_history_days": 1}, cutoff)["markets"][0]["source_access"]
    store.save_local_record("history_import", record)
    assert access()["status"] == "untested"
    store.save_local_record("history_import", {**record, "record_id": "empty", "capture_evidence": "public_backfill_adapter", "validated_rows": 0})
    assert access()["status"] == "untested"
    store.save_local_record("history_import", {**record, "record_id": "declared-owned", "capture_evidence": "public_backfill_adapter"})
    assert access()["status"] == "success" and access()["evidence"]["table"] == "local_records"
    assert access(market="ETH/EUR")["status"] == "untested"
    assert access(source="bitstamp")["status"] == "untested"
    assert access(cutoff=NOW - 1)["status"] == "untested"


def test_backfill_cli_configuration_ceiling_stops_before_fetch(tmp_path, monkeypatch, capsys):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    class Client:
        has = {"fetchOHLCV": True}
        session = requests.Session()

        def fetch_ohlcv(self, *_args, **_kwargs):
            pytest.fail("Configured request budget was consumed by catalogue")

    class Pool:
        def client(self, _source):
            return Client()

        def markets(self, _source, client):
            client.session.get(f"http://127.0.0.1:{server.server_port}/catalogue", timeout=1).raise_for_status()
            return {"BTC/EUR": {"symbol": "BTC/EUR", "base": "BTC", "quote": "EUR", "active": True, "spot": True}}

        def close(self):
            Client.session.close()

    monkeypatch.setattr("crypto_market_analysis.history_backfill.ExchangeClients", Pool)
    config = tmp_path / "observer.toml"
    config.write_text("http_max_requests = 1\nhttp_deadline_seconds = 5.0\n")
    request = {"source": "coinbase", "markets": [{"asset": "BTC", "quote": "EUR"}], "start": NOW - 3600, "end": NOW}
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request)))
    try:
        assert main(["backfill-history", "--mode", "import", "--apply", "--request", "-",
                     "--data-dir", str(tmp_path / "ordinary"), "--config", str(config)]) == 1
        result = json.loads(capsys.readouterr().out)
        assert hits == ["/catalogue"]
        assert result["cycle"]["http_budget"]["max_requests"] == 1
        assert result["cycle"]["http_budget"]["used_requests"] == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_actual_stdio_missing_readiness_and_backfill_status_remain_readonly(tmp_path):
    missing = tmp_path / "absent"

    async def check():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--mode", "import", "--data-dir", str(missing)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 64 and all(tool.annotations.readOnlyHint for tool in tools)
            for name, arguments in (("get_history_backfill_status", {}),
                                    ("get_analysis_readiness", {"request": {"dossier": dossier()}, "as_of": NOW})):
                result = await session.call_tool(name, arguments)
                assert not result.isError
    asyncio.run(check())
    assert not missing.exists()
