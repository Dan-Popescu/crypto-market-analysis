import asyncio

import pytest

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Observation
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store


def test_registered_discovery_and_history_share_configured_native_mode(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    store.put_observations([
        Observation("fixture", "BTC", "funding", 100, 100, 1, "rate", "USD", data_mode="demo"),
        Observation("fixture", "BTC", "funding", 100, 100, 2, "rate", "EUR", data_mode="live"),
    ])
    server = create_server(Settings(data_dir=tmp_path), "demo")

    async def check():
        tools = {tool.name: tool for tool in await server.list_tools()}
        required = {"get_metric_series", "get_result_page", "get_request_schema",
                    "get_dataset_status", "get_collection_history", "get_storage_status",
                    "get_upcoming_calendar"}
        assert required <= tools.keys()
        assert all(tools[name].annotations.readOnlyHint for name in required)
        response = await server.call_tool("get_metric_series", {"asset": "BTC", "as_of": 200})
        assert len(response.model_dump_json().encode()) <= 32768
        [series] = response.structuredContent["series"]
        assert series["dimension"]["quote"] == "USD"
        response = await server.call_tool("get_metric_history", {
            "asset": "BTC", "metric": "funding", "dimension": series["dimension"], "as_of": 200})
        assert [row["value"] for row in response.structuredContent["points"]] == [1]

    asyncio.run(check())


def test_engine_applies_configured_storage_budget_without_blocking_reads(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, storage_hot_max_bytes=1,
                             storage_write_reserve_bytes=1), "demo")
    with pytest.raises(ValueError, match="admission budget"):
        engine.store.put_observations([
            Observation("fixture", "BTC", "funding", 100, 100, 1, "rate", "USD", data_mode="demo")])
    assert engine.store.observations("BTC", as_of=200, mode="demo") == []
