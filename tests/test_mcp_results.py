"""Real MCP wire budgets and frozen, lossless read-only evidence access."""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from crypto_market_analysis.config import Settings
from crypto_market_analysis.mcp_results import (
    RESPONSE_BYTE_LIMIT,
    MCPResultPresenter,
    ResultCache,
    canonical_json,
    response_bytes,
)
from crypto_market_analysis.models import Observation
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store
from crypto_market_analysis.stored_analysis import analyze_stored_derivatives


def binding(summary):
    metadata = summary["_mcp_result"]
    return {key: metadata[key] for key in ("handle", "selection_hash", "content_hash")}


def test_compact_real_protocol_1000_records_and_frozen_pages(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(settings.database)
    now = 172800
    rows = [Observation(source="venue", asset="BTC", metric="open_interest_amount",
                        event_time=now - offset * 60, observed_at=now - offset * 60,
                        value=1000 - offset, unit="BTC", quote="USDT", data_mode="import",
                        metadata={"symbol": "BTC/USDT:USDT", "settle": "USDT", "market_type": "swap",
                                  "counting_convention": "one_sided"}) for offset in range(1000)]
    store.put_observations(rows)
    request = {"asset": "BTC", "source": "venue", "quote": "USDT", "data_mode": "import"}
    expected = analyze_stored_derivatives(store, request, now)
    server = create_server(settings, mode="import")

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            compact = await session.call_tool("get_stored_derivatives", {"request": request, "as_of": now})
            assert not compact.isError
            assert len(compact.model_dump_json(by_alias=True, exclude_none=True).encode()) < RESPONSE_BYTE_LIMIT
            summary = compact.structuredContent
            assert summary["series"] == expected["series"]
            assert summary["stored_inputs"]["consumed_records"]["count"] == 1000
            assert "/stored_inputs/consumed_records" in summary["_mcp_result"]["omitted_paths"]
            assert summary["_mcp_result"]["content_hash"] == hashlib.sha256(canonical_json(expected)).hexdigest()
            # A later database mutation cannot change the frozen result.
            store.put_observations([Observation(**{**rows[0].__dict__, "value": 7777,
                                                    "observed_at": now + 1})])
            database_before_pages = settings.database.read_bytes()
            files_before_pages = sorted(path.name for path in tmp_path.iterdir())
            collected = []
            offset = 0
            while offset is not None:
                result = await session.call_tool("get_result_page", {**binding(summary),
                    "path": "/stored_inputs/consumed_records", "offset": offset, "limit": 100})
                assert not result.isError
                assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) < RESPONSE_BYTE_LIMIT
                page = result.structuredContent
                collected.extend(page["items"])
                offset = page["next_offset"]
            assert collected == expected["stored_inputs"]["consumed_records"]
            assert settings.database.read_bytes() == database_before_pages
            assert sorted(path.name for path in tmp_path.iterdir()) == files_before_pages
            absent = await session.call_tool("get_result_page", {**binding(summary), "path": "/no-such-path"})
            assert absent.isError and "not found" in absent.content[0].text
            wrong = await session.call_tool("get_result_page", {**binding(summary), "selection_hash": "different"})
            assert wrong.isError and "mismatch" in wrong.content[0].text

    asyncio.run(check())


def test_oversize_unicode_fragment_roundtrip_and_complete_result_hash():
    presenter = MCPResultPresenter()
    raw = {"records": [{"source": "unicode", "value": "💶é漢字\\\"\n" * 10000}], "uncertainty": None}
    summary = presenter.present(raw, {"tool": "fixture"})
    args = binding(summary)
    chunks = []
    fragment_offset = 0
    while True:
        page = presenter.page(**args, path="/records", fragment_offset=fragment_offset)
        assert response_bytes(page) < RESPONSE_BYTE_LIMIT
        assert page["fragment_encoding"] == "canonical-json-text"
        chunks.append(page["fragment"])
        fragment_offset = page["next_fragment_offset"]
        if fragment_offset is None:
            assert page["next_offset"] is None
            break
    assert json.loads("".join(chunks)) == raw["records"][0]
    # Root object paging and fragments allow exact full-result reconstruction.
    rebuilt = {}
    offset = 0
    while offset is not None:
        page = presenter.page(**args, offset=offset)
        if "items" in page:
            rebuilt.update((entry["key"], entry["value"]) for entry in page["items"])
        else:
            chunks = [page["fragment"]]
            while page["next_fragment_offset"] is not None:
                page = presenter.page(**args, offset=offset, fragment_offset=page["next_fragment_offset"])
                chunks.append(page["fragment"])
            entry = json.loads("".join(chunks))
            rebuilt[entry["key"]] = entry["value"]
        offset = page["next_offset"]
    assert rebuilt == raw
    assert hashlib.sha256(canonical_json(rebuilt)).hexdigest() == args["content_hash"]


def test_100_analytical_rows_keep_uncertainty_or_explicit_omission():
    rows = [{"rank": index, "amount": str(index * 100), "uncertainty": {"lower": None, "upper": None},
             "limits": ["insufficient support; never calibrated"] * 10} for index in range(100)]
    presenter = MCPResultPresenter()
    raw = {"results": rows, "missing": None, "data_mode": "demo"}
    summary = presenter.present(raw, {"tool": "fixture"})
    assert response_bytes(summary) < RESPONSE_BYTE_LIMIT
    assert summary["missing"] is None
    assert summary["results"]["_omitted"] is True
    assert summary["results"]["count"] == 100
    assert "/results" in summary["_mcp_result"]["omitted_paths"]
    collected, offset = [], 0
    while offset is not None:
        page = presenter.page(**binding(summary), path="/results", offset=offset)
        collected.extend(page["items"])
        offset = page["next_offset"]
    assert collected == rows


def test_expiry_lru_memory_and_selection_binding():
    now = [0.0]
    cache = ResultCache(ttl=5, max_bytes=80, max_entries=2, clock=lambda: now[0], wall_clock=lambda: now[0])
    one, first = cache.put({"value": "a" * 10}, {"source": "one"})
    two, second = cache.put({"value": "b" * 10}, {"source": "two"})
    cache.get(one, first.selection_hash, first.content_hash)
    three, third = cache.put({"value": "c" * 10}, {"source": "three"})
    with pytest.raises(ValueError, match="evicted"):
        cache.get(two, second.selection_hash, second.content_hash)
    assert cache._bytes <= 80
    with pytest.raises(ValueError, match="mismatch"):
        cache.get(one, third.selection_hash, first.content_hash)
    now[0] = 6
    with pytest.raises(ValueError, match="expired"):
        cache.get(three, third.selection_hash, third.content_hash)
    assert cache._bytes == 0
    with pytest.raises(ValueError, match="capacity"):
        cache.put({"value": "z" * 81}, {})


def test_concurrent_cache_and_pages_detach_mutations():
    presenter = MCPResultPresenter(ResultCache(max_entries=100))
    raw = {"rows": [{"index": index} for index in range(200)]}
    summary = presenter.present(raw, {})
    raw["rows"][0]["index"] = -1
    def read(index):
        presenter.present({"other": index}, {"index": index})
        return presenter.page(**binding(summary), path="/rows", offset=index, limit=1)["items"][0]["index"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(read, range(40))) == list(range(40))


@pytest.mark.parametrize("kwargs", [{"limit": 101}, {"limit": 0}, {"offset": -1},
                                    {"offset": True}, {"fragment_offset": -1},
                                    {"path": "../../tmp"}, {"path": "/a~2"}, {"offset": 999}])
def test_invalid_page_bounds(kwargs):
    presenter = MCPResultPresenter()
    summary = presenter.present({"rows": [1]}, {})
    with pytest.raises(ValueError):
        presenter.page(**binding(summary), **kwargs)


def test_history_preserves_points_cursor_and_core_when_metadata_is_huge():
    presenter = MCPResultPresenter()
    rows = [{"value": index, "source": "venue", "unit": "BTC", "quote": "USD",
             "event_time": index, "metadata": {"raw": "x" * 50000}} for index in range(3)]
    raw = {"points": rows, "next_cursor": "frozen-backend-page", "remaining_points": 7,
           "truncated": True, "series_count": 1}
    summary = presenter.present(raw, {"tool": "get_metric_history"})
    assert isinstance(summary["points"], list) and len(summary["points"]) == 3
    assert summary["next_cursor"] == raw["next_cursor"]
    assert summary["remaining_points"] == 7
    assert [row["value"] for row in summary["points"]] == [0, 1, 2]
    assert all(row["metadata"]["_omitted"] for row in summary["points"])
    for index in range(3):
        assert f"/points/{index}/metadata" in summary["_mcp_result"]["omitted_paths"]
    assert response_bytes(summary) < RESPONSE_BYTE_LIMIT


def test_100_rows_uncertainty_real_mcp_protocol():
    presenter = MCPResultPresenter()
    server = FastMCP("bounded fixture")
    rows = [{"rank": index, "amount": str(index * 100), "uncertainty": {"lower": None, "upper": None},
             "limits": ["insufficient support; never calibrated"] * 10} for index in range(100)]

    @presenter.tool(server)
    def fixture() -> dict:
        return {"results": rows, "unknown": None}

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("fixture", {})
            assert not result.isError
            assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) < RESPONSE_BYTE_LIMIT
            summary = result.structuredContent
            assert summary["unknown"] is None
            assert summary["results"]["count"] == 100
            assert "/results" in summary["_mcp_result"]["omitted_paths"]
    asyncio.run(check())
