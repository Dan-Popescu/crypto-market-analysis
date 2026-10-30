"""Repair cycles, stored operational reads and MCP keep the same identities."""

import asyncio
import json

from crypto_market_analysis.automatic_repair import run_auto_repairs
from crypto_market_analysis.capacity_cli import capacity_exit_code, render_capacity_report
from crypto_market_analysis.config import Settings
from crypto_market_analysis.dataset_lock import dataset_writer_lock
from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset, RESTCapture
from crypto_market_analysis.http_accounting import stored_http_accounting
from crypto_market_analysis.server import create_server

NOW = 1000 * 3600


def test_mixed_repair_outcomes_keep_accounting_run_links_and_unknown_totals_in_mcp(tmp_path):
    ds = HistoryDataset(DatasetConfig("synthetic-interfaces", str(tmp_path), NOW - 3600, NOW), clock=lambda: NOW)

    def fetch(asset, quote, since, _limit):
        if asset == "BTC":
            raise RuntimeError("https://example.invalid/private?secret=hidden; body=confidential")
        return RESTCapture([[since * 1000, 10, 10, 10, 10, 1]])

    request = {"markets": [["BTC", "EUR"], ["ETH", "USD"]], "request_budget": 2, "max_hours": 2}
    result = run_auto_repairs(ds.store.path, request, apply=True, fetch=fetch, clock=lambda: NOW)
    assert result["requests_attempted"] == 2
    assert result["http_accounting"]["coverage"] == "uninstrumented"
    assert result["http_accounting"]["totals"] is None
    assert result["http_accounting"]["uninstrumented_runs"] == 2
    ids = {row["last_http_accounting_run_id"] for row in result["outcomes"]}
    assert len(ids) == 2 and None not in ids
    assert all(row["last_http_accounting_retention"] == "retained" for row in result["outcomes"])
    assert "hidden" not in json.dumps(result) and "confidential" not in json.dumps(result)
    assert capacity_exit_code("auto-repair-history", result, apply=True) == 1
    rendered = render_capacity_report(result, "auto-repair-history")
    assert "observed attempts: unknown" in rendered and "| ETH | USD |" in rendered
    accounting = stored_http_accounting(ds.store.path, mode="import", task="history_rest", end=NOW)
    assert accounting["included_runs"] == 2 and accounting["summary"]["totals"] is None
    assert stored_http_accounting(ds.store.path, mode="live", end=NOW)["included_runs"] == 0
    with ds.store.connection() as con:
        assert {r[0] for r in con.execute("SELECT id FROM runs")} == ids
        rows_before = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]

    async def check():
        server = create_server(Settings(data_dir=tmp_path), "import")
        tools = await server.list_tools()
        assert len(tools) == 64 and all(tool.annotations.readOnlyHint for tool in tools)
        with dataset_writer_lock(ds.store.path):
            for name, arguments in (("get_http_accounting", {"task": "history_rest", "end": NOW}),
                                    ("get_auto_repair_status", {})):
                response = await server.call_tool(name, arguments)
                assert not response.isError
                payload = response.structuredContent
                if name == "get_http_accounting":
                    assert payload["included_runs"] == 2 and payload["summary"]["totals"] is None
                else:
                    assert payload["scheduling"]["next_market"] == ["BTC", "EUR"]
                    assert {row["last_http_accounting_run_id"] for row in payload["tasks"]} == ids
                assert len(response.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768

    asyncio.run(check())
    with ds.store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == rows_before == 1
        assert {r[0] for r in con.execute("SELECT id FROM runs")} == ids


def test_busy_apply_exit_is_unsuccessful_and_markdown_explains_no_capture(tmp_path):
    ds = HistoryDataset(DatasetConfig("synthetic-busy", str(tmp_path), NOW - 3600, NOW), clock=lambda: NOW)

    def forbidden(*_):
        raise AssertionError("Busy apply must not call the fetcher")

    with dataset_writer_lock(ds.store.path):
        result = run_auto_repairs(ds.store.path, apply=True, fetch=forbidden, clock=lambda: NOW)
    assert result["status"] == "busy" and result["requests_attempted"] == 0
    assert capacity_exit_code("auto-repair-history", result, apply=True) == 1
    assert "no capture or recovery" in render_capacity_report(result, "auto-repair-history")
    assert result["applied"] is False
