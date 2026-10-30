"""Actual MCP startup and reads must never initialize or migrate configured data."""
import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.history_dataset import DatasetConfig
from crypto_market_analysis.models import Observation
from crypto_market_analysis.request_schemas import PLAN_EXAMPLE
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store


async def _missing_store_checks(session):
    tools = {tool.name for tool in (await session.list_tools()).tools}
    assert {"get_dataset_status", "get_storage_status", "get_request_schema", "preview_purchase_funding"} <= tools
    for name, arguments in (("get_dataset_status", {}), ("get_storage_status", {}),
                            ("get_collection_history", {}), ("get_upcoming_calendar", {}),
                            ("get_request_schema", {"tool_name": "construct_purchase_plan"}),
                            ("preview_purchase_funding", {"config": PLAN_EXAMPLE, "as_of": PLAN_EXAMPLE["start_time"]})):
        result = await session.call_tool(name, arguments)
        assert not result.isError, (name, result)
    missing = await session.call_tool("get_analysis_bundle", {})
    assert missing.isError
    assert "Configured database is missing" in missing.content[0].text


def test_create_server_real_sdk_missing_store_has_no_filesystem_side_effects(tmp_path):
    directory = tmp_path / "not-created"
    server = create_server(Settings(data_dir=directory))
    assert not directory.exists()

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            await _missing_store_checks(session)
    asyncio.run(check())
    assert not directory.exists()


def test_stdio_serve_missing_store_never_constructs_writable_engine(tmp_path):
    directory = tmp_path / "not-created"

    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--data-dir", str(directory)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            await _missing_store_checks(session)
    asyncio.run(check())
    assert not directory.exists()


def test_existing_import_store_mcp_does_not_migrate_or_recover_pending(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    store.put_observations([Observation("fixture", "BTC", "funding", 100, 100, 1, "rate", "USD", data_mode="import")])
    with store.connection() as con:
        con.execute("DROP INDEX observation_history")
        con.execute("PRAGMA user_version=1")
        # A deliberately old dimension would be rewritten by writable migration.
        con.execute("UPDATE observations SET dimension='{}'")
    config = DatasetConfig("synthetic", str(tmp_path), 0, 3600)
    (tmp_path / "dataset.json").write_text(json.dumps(asdict(config)))
    pending = tmp_path / "pending-unapplied.json"
    pending.write_text('{"unapplied":"must not recover"}')
    before = store.path.read_bytes()
    server = create_server(Settings(data_dir=tmp_path), "import")

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("get_metric_history", {"asset": "BTC", "metric": "funding", "as_of": 200})
            assert not result.isError
            assert result.structuredContent["points"][0]["value"] == 1
    asyncio.run(check())
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert con.execute("SELECT dimension FROM observations").fetchone()[0] == "{}"
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='observation_history'").fetchone()
    assert pending.read_text() == '{"unapplied":"must not recover"}'


def test_readonly_store_sees_new_wal_commits_and_rejects_writes(tmp_path):
    writer = Store(tmp_path / "observer.sqlite")
    reader = Engine(Settings(data_dir=tmp_path), initialize=False).store
    with sqlite3.connect(writer.path) as pinned:
        pinned.execute("PRAGMA wal_autocheckpoint=0")
        pinned.execute("SELECT COUNT(*) FROM observations").fetchone()
        writer.put_observations([Observation("fixture", "BTC", "funding", 100, 100, 2, "rate", "USD")])
        assert writer.path.with_name(writer.path.name + "-wal").stat().st_size > 0
        assert reader.metric_history("BTC", "funding", as_of=200)["points"][0]["value"] == 2
        with reader.connection() as con:
            assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(ValueError, match="read failed.*readonly"):
            with reader.connection() as con:
                con.execute("CREATE TABLE forbidden(id INTEGER)")


def test_readonly_old_or_empty_schema_errors_without_initialization(tmp_path):
    database = tmp_path / "observer.sqlite"
    database.touch()
    store = Store(database, initialize=False)
    with pytest.raises(ValueError, match="without initialization or migration.*no such table"):
        store.counts()
    assert database.read_bytes() == b""
