"""Operational reads never initialize/recover datasets or merge market modes."""
import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import jsonschema
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from crypto_market_analysis.config import Settings
from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset
from crypto_market_analysis.operational_reads import (
    collection_history,
    dataset_status,
    register_operational_tools,
    storage_status,
    upcoming_calendar,
)
from crypto_market_analysis.request_schemas import REGISTRY, get_request_schema
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store


def test_missing_reads_create_no_files(tmp_path):
    directory = tmp_path / "absent"
    database = directory / "observer.sqlite"
    assert dataset_status(database, "import")["status"] == "not_configured"
    assert collection_history(database, "live")["rows"] == []
    assert upcoming_calendar(database, "live")["rows"] == []
    assert storage_status(database, "live")["physical"]["database_bytes"] == 0
    assert not directory.exists()


def test_dataset_read_does_not_recover_pending_or_initialize_database(tmp_path, monkeypatch):
    config = DatasetConfig("synthetic", str(tmp_path), 0, 7200)
    marker = tmp_path / "dataset.json"
    marker.write_text(json.dumps(asdict(config)))
    pending = tmp_path / "pending-synthetic.json"
    pending.write_text('{"recovery": "must remain untouched"}')
    monkeypatch.setattr(HistoryDataset, "__init__", lambda *args, **kwargs: pytest.fail("constructor called"))
    database = tmp_path / "observer.sqlite"
    assert dataset_status(database, "import")["status"] == "database_unavailable"
    assert not database.exists()
    assert pending.read_text() == '{"recovery": "must remain untouched"}'


def _dataset(tmp_path):
    config = DatasetConfig("synthetic", str(tmp_path), 0, 7200)
    (tmp_path / "dataset.json").write_text(json.dumps(asdict(config)))
    store = Store(tmp_path / "observer.sqlite")
    with store.connection() as con:
        con.executescript("""CREATE TABLE history_captures(id TEXT PRIMARY KEY, asset TEXT, quote TEXT, materialized_at INTEGER, payload TEXT);
            CREATE TABLE history_attempts(id INTEGER PRIMARY KEY,asset TEXT,quote TEXT,payload TEXT);
            CREATE TABLE history_repairs(market TEXT PRIMARY KEY,payload TEXT);""")
        for i in range(4):
            payload = {"id": str(i), "source": "kraken", "data_mode": "import", "conflicts": [{"open_time": i, "existing": "x"}]}
            con.execute("INSERT INTO history_captures VALUES(?,?,?,?,?)", (str(i), "BTC", "EUR", 10 + i, json.dumps(payload)))
            con.execute("INSERT INTO history_attempts VALUES(?,?,?,?)", (i, "BTC", "EUR", json.dumps({"attempted_at": 10 + i})))
    return store


def test_dataset_pages_freeze_cutoff_and_highwater_and_preserve_conflicts(tmp_path):
    store = _dataset(tmp_path)
    first = dataset_status(store.path, "import", section="captures", as_of=12, limit=1)
    assert first["rows"][0]["id"] == "0"
    assert "conflicts" not in first["rows"][0]
    assert first["rows"][0]["conflicts_total"] == 1
    with store.connection() as con:
        con.execute("INSERT INTO history_captures VALUES(?,?,?,?,?)", ("late", "BTC", "EUR", 11, '{"id":"late"}'))
    second = dataset_status(store.path, "import", section="captures", limit=100, cursor=first["pagination"]["next_cursor"])
    assert [r["id"] for r in second["rows"]] == ["1", "2"]
    with pytest.raises(ValueError, match="cursor"):
        dataset_status(store.path, "import", section="attempts", cursor=first["pagination"]["next_cursor"])
    conflicts = dataset_status(store.path, "import", section="conflicts", as_of=12, limit=2)
    assert [r["capture_id"] for r in conflicts["rows"]] == ["0", "1"]
    assert dataset_status(store.path, "live")["status"] == "selection_mismatch"
    assert dataset_status(store.path, "import", source="other")["rows"] == []
    before = dataset_status(store.path, "import", as_of=10)
    after = dataset_status(store.path, "import", as_of=12)
    assert before["counts"]["captures"] == 1
    assert before["dataset_version"] != after["dataset_version"]
    with pytest.raises(ValueError, match="mutable"):
        dataset_status(store.path, "import", section="repairs", as_of=12)


def test_collection_modes_legacy_and_current_scheduler_state_are_separate(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    with store.connection() as con:
        for i, mode in enumerate(("live", "demo", None, "live")):
            payload = {"data_mode": mode, "observed_at": 10 + i, "errors": [], "source": "a"}
            con.execute("INSERT INTO runs(source,task,observed_at,payload) VALUES(?,?,?,?)", ("a", "market", 10 + i, json.dumps(payload)))
        con.execute("CREATE TABLE scheduler_state(source TEXT,task TEXT,next_due REAL)")
        con.execute("INSERT INTO scheduler_state VALUES('a','market',123)")
    result = collection_history(store.path, "live", as_of=12)
    assert [r["data_mode"] for r in result["rows"]] == ["live"]
    assert result["excluded_mode_unknown_runs"] == 1
    assert collection_history(store.path, "live", as_of=12, section="unclassified_runs")["rows"][0]["data_mode"] is None
    assert collection_history(store.path, "live", source="other")["rows"] == []
    assert collection_history(store.path, "live", section="state")["rows"][0]["next_due"] == 123
    with pytest.raises(ValueError, match="unversioned"):
        collection_history(store.path, "live", section="state", as_of=10)


def test_calendar_versions_cancellations_unknown_clocks_and_paging(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    rows = [
        {"event_id": "a", "event_time": 100, "observed_at": 1, "source": "x", "status": "planned"},
        {"event_id": "a", "event_time": 100, "observed_at": 5, "source": "x", "status": "cancelled"},
        {"event_id": "b", "event_time": 110, "observed_at": 1, "source": "x", "data_mode": "demo"},
        {"event_id": "c", "event_time": 120, "observed_at": 1, "source": "x", "data_mode": "live"},
        {"event_id": "d", "event_time": None, "local_date": "1970-01-01", "observed_at": 1, "source": "x"},
    ]
    for row in rows:
        store.save_local_record("calendar", row)
    first = upcoming_calendar(store.path, "live", source="x", as_of=4, start=0, end=200, limit=1)
    assert first["rows"][0]["event_id"] == "d"
    assert first["rows"][0]["event_time"] is None
    second = upcoming_calendar(store.path, "live", source="x", start=0, end=200, cursor=first["pagination"]["next_cursor"])
    assert [r["event_id"] for r in second["rows"]] == ["a", "c"]
    latest = upcoming_calendar(store.path, "live", source="x", as_of=5, start=0, end=200)
    assert [r["event_id"] for r in latest["rows"]] == ["d", "c"]
    with pytest.raises(ValueError, match="366"):
        upcoming_calendar(store.path, "live", start=0, end=367 * 86400)


def test_all_request_examples_match_schema_and_actual_tool_validators(tmp_path):
    Store(tmp_path / "observer.sqlite")
    server = create_server(Settings(data_dir=tmp_path), mode="demo")

    async def run():
        actual = {t.name: t for t in await server.list_tools()}
        for name, contract in REGISTRY.items():
            assert name in actual
            schema = contract["input_schema"]
            assert set(schema["properties"]) <= set(actual[name].inputSchema["properties"])
            for example in contract["examples"]:
                jsonschema.Draft202012Validator(schema).validate(example)
                await server.call_tool(name, example)
    asyncio.run(run())


def test_contract_discovery_is_bounded_independent_and_explicit_about_fees():
    names = get_request_schema("list")["tools"]
    assert "calculate_purchase_costs" in names
    plan = get_request_schema("preview_purchase_funding")
    assert plan["input_schema"]["properties"]["config"]["properties"]["fees"]["properties"]["rate"]["default"] == "0"
    costs = get_request_schema("calculate_purchase_costs")
    fee_fields = costs["input_schema"]["properties"]["request"]["properties"]["snapshots"]["items"]["properties"]["assumptions"]["anyOf"][0]
    assert "No fee defaults" in fee_fields["description"]
    plan["examples"].clear()
    assert get_request_schema("preview_purchase_funding")["examples"]
    with pytest.raises(ValueError, match="Unknown"):
        get_request_schema("../../arbitrary")


def test_operational_registration_actual_tool_calls_remain_read_only(tmp_path):
    from crypto_market_analysis.storage_policy import StoragePolicy

    server = FastMCP("operational test")
    settings = SimpleNamespace(database=tmp_path / "missing" / "observer.sqlite",
                               **{f"storage_{k}": v for k, v in asdict(StoragePolicy()).items()})
    engine = SimpleNamespace(settings=settings, mode="live")
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    register_operational_tools(server, engine, readonly)

    async def run():
        tools = {tool.name: tool for tool in await server.list_tools()}
        expected = {"get_request_schema", "get_dataset_status", "get_collection_history", "get_storage_status", "get_upcoming_calendar"}
        assert set(tools) == expected
        assert all(tool.annotations.readOnlyHint for tool in tools.values())
        assert "database" not in str([tool.inputSchema for tool in tools.values()])
        for name in expected:
            await server.call_tool(name, {"tool_name": "list"} if name == "get_request_schema" else {})
    asyncio.run(run())
    assert not settings.database.parent.exists()


def test_documented_plan_defaults_follow_actual_normalization():
    from crypto_market_analysis.plans import validate_config
    from crypto_market_analysis.request_schemas import PLAN_EXAMPLE

    example = {k: v for k, v in PLAN_EXAMPLE.items() if k not in ("fees", "execution")}
    normalized = validate_config(example)
    schema = get_request_schema("compare_purchase_plans")["input_schema"]["properties"]["config"]["properties"]
    for group in ("fees", "execution"):
        for field, definition in schema[group]["properties"].items():
            assert normalized[group][field] == definition["default"]
