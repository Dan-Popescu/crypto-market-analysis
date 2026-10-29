"""Local evidence connections through real CLI and a single read-only MCP session."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from crypto_market_analysis.cli import main
from crypto_market_analysis.collectors import ExchangeClients
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle

ROOT = Path(__file__).resolve().parents[1]
NOW = 1793145600


class NativePublicVenue:
    has = {"fetchOrderBook": True}

    def __init__(self):
        self.book_calls = []

    def load_markets(self):
        return {"BTC/EUR": {"base": "BTC", "quote": "EUR", "spot": True, "symbol": "BTC/EUR"}}

    def fetch_order_book(self, symbol, limit=100):
        self.book_calls.append((symbol, limit))
        return {"timestamp": NOW * 1000, "bids": [[99, 2]], "asks": [[101, 3]]}


class OfficialBytes:
    def __init__(self):
        self.calls = []

    def get_bytes(self, url):
        self.calls.append(url)
        return (b'<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
                b'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
                b'<Cube><Cube time="2026-10-27"><Cube currency="USD" rate="1.10"/>'
                b'</Cube></Cube></gesmes:Envelope>')


@pytest.fixture
def connected(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=tmp_path / "market"))
    monkeypatch.setattr("time.time", lambda: NOW)
    venue = NativePublicVenue()
    clients = ExchangeClients()
    clients._clients["kraken"] = venue
    books = engine.capture_books(sources=["kraken"], assets=["BTC"], quotes=["EUR"], clients=clients)
    assert books["inserted_books"] == 1
    assert venue.book_calls == [("BTC/EUR", 500)]
    http = OfficialBytes()
    official = engine.collect_official_context(include=("fx",), client=http)
    assert official["inserted"]["fx_reference"] == 1
    assert official["fx_reference"]["event_time"] is None
    assert len(http.calls) == 1
    end = NOW // 3600 * 3600
    engine.store.put_candles([
        Candle("kraken", "BTC", "EUR", "1h", start, start + 3600,
               100, 101, 99, 100, 1, start + 3600)
        for start in range(end - 86400, end, 3600)
    ])
    return engine


def requests():
    market = {"asset": "BTC", "quote": "EUR", "sources": ["kraken"], "data_mode": "live"}
    return {
        "stored-derivatives": {"asset": "BTC", "quote": "USDT", "source": "binanceusdm", "data_mode": "live"},
        "stored-quotes": {"quote": "EUR", "sources": ["ecb"], "data_mode": "live"},
        "stored-venue-costs": {**market, "budget": "100"},
        "stored-venue-disagreement": market,
        "qualify-sources": {"sources": ["kraken"], "markets": ["BTC/EUR"], "required_history_days": 1},
        "local-workflow": {"sources": ["kraken"], "markets": ["BTC/EUR"], "required_history_days": 1,
                           "refresh": False, "as_of": NOW},
    }


def test_real_cli_reads_connected_sqlite_json_and_tailored_markdown(connected, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    expected = {"stored-derivatives": "Native change", "stored-quotes": "unknown_timestamp",
                "stored-venue-costs": "Declared all-in budget", "stored-venue-disagreement": "Median deviation",
                "qualify-sources": "Missing hours", "local-workflow": "Refresh requested: False"}
    for command, request in requests().items():
        path = tmp_path / f"{command}.json"
        path.write_text(json.dumps(request))
        args = [sys.executable, "-m", "crypto_market_analysis", command, "--request", str(path),
                "--data-dir", str(connected.settings.data_dir), "--as-of", str(NOW)]
        result = subprocess.run(args, capture_output=True, text=True, env=env, check=True)
        payload = json.loads(result.stdout)
        assert payload["as_of"] == NOW
        if command.startswith("stored-"):
            assert payload["stored_inputs"]["read_only"] is True
        report = subprocess.run([*args, "--format", "markdown"], capture_output=True, text=True, env=env, check=True)
        assert expected[command] in report.stdout
    costs = connected.stored_venues({**requests()["stored-venue-costs"], "analysis": "purchase_costs"}, NOW)
    assert costs["rows"][0]["hypothetical_fill"] is None
    with connected.store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM local_records WHERE kind='fx_reference'").fetchone()[0] == 1


def test_new_mcp_tools_share_one_stdio_session_and_never_expose_writes(connected):
    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--data-dir", str(connected.settings.data_dir)],
            env={"PYTHONPATH": str(ROOT / "src")})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {tool.name for tool in tools}
            assert not {"capture_books", "collect_official_context", "local_workflow"} & names
            mapping = {"stored-derivatives": "get_stored_derivatives", "stored-quotes": "get_stored_quotes",
                       "stored-venue-costs": "get_stored_venue_costs",
                       "stored-venue-disagreement": "get_stored_venue_disagreement",
                       "qualify-sources": "qualify_sources"}
            for command, name in mapping.items():
                tool = next(tool for tool in tools if tool.name == name)
                assert tool.annotations.readOnlyHint is True
                result = await session.call_tool(name, {"request": requests()[command], "as_of": NOW})
                assert not result.isError, result.content
                assert result.structuredContent["as_of"] == NOW
            invalid = await session.call_tool("get_stored_quotes", {"request": {"path": "/tmp/private"}})
            assert invalid.isError
    asyncio.run(run())


@pytest.mark.parametrize("command,args", [
    ("capture-books", ["--sources", "kraken", "--assets", "BTC", "--quotes", "EUR"]),
    ("collect-official-context", []),
])
def test_capture_rejects_retiming_or_synthetic_modes_before_store_creation(tmp_path, capsys, command, args):
    for forbidden in (["--as-of", str(NOW)], ["--mode", "demo"], ["--mode", "import"]):
        data_dir = tmp_path / "unused"
        assert main([command, *args, *forbidden, "--data-dir", str(data_dir)]) == 2
        assert not data_dir.exists()
        assert "Network capture requires" in capsys.readouterr().err


def test_report_output_protects_evidence_and_request(connected, tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(requests()["local-workflow"]))
    for destination in (connected.settings.database, request):
        before = destination.read_bytes()
        assert main(["local-workflow", "--request", str(request), "--data-dir", str(connected.settings.data_dir),
                     "--output", str(destination)]) == 2
        assert destination.read_bytes() == before
        assert "must not overwrite" in capsys.readouterr().err


def test_named_cli_selectors_preserve_native_market_and_reject_conflicts(connected, tmp_path, capsys):
    args = ["stored-venue-disagreement", "--sources", "kraken", "--asset", "BTC", "--quote", "EUR",
            "--data-dir", str(connected.settings.data_dir), "--as-of", str(NOW)]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stored_inputs"]["selection"] == {
        "asset": "BTC", "quote": "EUR", "sources": ["kraken"], "data_mode": "live"}
    request = tmp_path / "selectors.json"
    request.write_text(json.dumps({**requests()["stored-venue-disagreement"], "quote": "USD"}))
    assert main([*args, "--request", str(request)]) == 2
    assert "--quote conflicts" in capsys.readouterr().err


def test_named_import_dataset_uses_request_mode_without_global_mode_flag(tmp_path):
    from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset

    end = NOW // 3600 * 3600
    dataset = HistoryDataset(DatasetConfig("native-import", str(tmp_path / "dataset"), end - 86400, end))
    dataset.store.put_candles([
        Candle("kraken", "BTC", "EUR", "1h", start, start + 3600,
               100, 101, 99, 100, 1, start + 3600, data_mode="import")
        for start in range(end - 86400, end, 3600)
    ])
    counts_before = dataset.store.counts()
    identity_before = (dataset.directory / "dataset.json").read_bytes()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    for command, request in requests().items():
        request = {**request, "data_mode": "import"}
        path = tmp_path / f"{command}.json"
        path.write_text(json.dumps(request))
        output = subprocess.run([
            sys.executable, "-m", "crypto_market_analysis", command, "--request", str(path),
            "--data-dir", str(dataset.directory), "--as-of", str(NOW),
        ], capture_output=True, text=True, env=env, check=True)
        result = json.loads(output.stdout)
        assert result["data_mode"] == "import"
        assert result["as_of"] == NOW
        if command in ("qualify-sources", "local-workflow"):
            assert result["markets"][0]["data_mode"] == "import"
        if command == "qualify-sources":
            assert result["markets"][0]["coverage"]["available_hours"] == 24
        if command == "local-workflow":
            assert result["refresh"] is False
            assert result["issuance_requested"] is False
            assert result["collection"]["network_requested"] is False
            row = result["markets"][0]
            assert row["issuance"]["status"] == "not_requested"
            reference = row["accumulation_brief"]["input_reference"]
            assert reference["data_mode"] == "import"
            assert reference["dataset_lineage"] == {
                "name": "native-import", "source": "kraken", "data_mode": "import",
                "target_start": end - 86400, "target_end": end,
            }
    assert dataset.store.counts() == counts_before
    assert (dataset.directory / "dataset.json").read_bytes() == identity_before
    with dataset.store.connection() as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None
