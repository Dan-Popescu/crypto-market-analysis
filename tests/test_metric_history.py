"""Public history selection and real MCP continuation preserve native known-at series."""

import asyncio
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from crypto_market_analysis.config import Settings
from crypto_market_analysis.evidence import metric_dimension
from crypto_market_analysis.models import Observation
from crypto_market_analysis.retention import compact_liquidity
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store

NOW = 1_700_000_000


def point(event_time=NOW, **overrides):
    values = dict(source="venue", asset="BTC", metric="funding", event_time=event_time,
                  observed_at=NOW, value=1, unit="percent", quote="USD",
                  metadata={"instrument": "perpetual", "funding_interval_hours": 8})
    return Observation(**{**values, **overrides})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "observer.sqlite")


def test_sparse_selection_is_not_lost_behind_global_cap(store, monkeypatch):
    store.put_observations([point(NOW - 100, value=1), point(NOW - 101, value=2)])
    store.put_observations([point(NOW - offset, metric="noise") for offset in range(20)])
    # Reduce the former 200k generic cap to 2: even all requested points fit that
    # budget, yet the old MCP implementation lost them behind unrelated metrics.
    old_reader = store.observations
    monkeypatch.setattr(Store, "observations", lambda *args, **kwargs: old_reader(limit=2, **kwargs))
    assert all(row.metric == "noise" for row in store.observations(as_of=NOW))
    result = store.metric_history("BTC", "funding", as_of=NOW)
    assert [row["value"] for row in result["points"]] == [1, 2]
    assert result["truncated"] is False
    assert result["remaining_points"] == 0


def test_all_native_selectors_are_applied_before_limit(store):
    wanted = point(NOW - 10)
    store.put_observations([wanted, point(source="other"), point(quote="EUR"), point(unit="ratio"),
                            point(metadata={"instrument": "quarterly"}), point(data_mode="demo")])
    result = store.metric_history("BTC", "funding", "venue", 1, quote="USD", unit="percent",
                                  dimension=metric_dimension(wanted), as_of=NOW)
    assert [row["event_time"] for row in result["points"]] == [NOW - 10]
    assert result["series_count"] == 1
    assert result["truncated"] is False
    discovery = store.metric_series("BTC", "funding", as_of=NOW, limit=2)
    assert len(discovery["series"]) == 2
    assert discovery["truncated"] is True
    assert discovery["omitted_series"] == 3
    mixed = store.metric_history("BTC", "funding", as_of=NOW)
    assert mixed["multiple_series"] is True
    assert mixed["series_count"] == 5


def test_continuation_handles_equal_clocks_revisions_and_interleaved_receipts(store):
    originals = [point(NOW - offset, value=offset) for offset in range(5)]
    originals += [point(NOW, source="another", value=10)]
    store.put_observations(originals)
    store.put_observations([replace(originals[0], value=99)])  # same known-at second, higher id wins
    first = store.metric_history("BTC", "funding", limit=2, as_of=NOW)
    assert first["remaining_points"] == 4
    store.put_observations([replace(originals[2], value=88), point(NOW - 2, source="late", value=77),
                            point(NOW - 3, source="future", observed_at=NOW + 1)])
    pages = [first]
    while pages[-1]["next_cursor"]:
        pages.append(store.metric_history("BTC", "funding", limit=2, as_of=NOW,
                                          cursor=pages[-1]["next_cursor"]))
    flattened = [row for page in pages for row in page["points"]]
    assert len(flattened) == 6
    assert len({(row["source"], row["event_time"]) for row in flattened}) == 6
    assert {row["value"] for row in flattened} == {1, 2, 3, 4, 10, 99}
    assert pages[-1]["truncated"] is False
    fresh = store.metric_history("BTC", "funding", as_of=NOW)
    assert 88 in {row["value"] for row in fresh["points"]}
    assert all(row["source"] != "future" for row in fresh["points"])


def test_initial_watermark_and_rows_share_one_sqlite_snapshot(store, monkeypatch):
    store.put_observations([point(value=1)])
    writer = Store(store.path)
    original = store.connection
    inserted = False

    class InterleavedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, values=()):
            nonlocal inserted
            result = self.connection.execute(sql, values)
            if "MAX(id)" in sql and not inserted:
                inserted = True
                writer.put_observations([point(value=99), point(NOW - 1, source="late")])
            return result

    @contextmanager
    def interleaved():
        with original() as connection:
            yield InterleavedConnection(connection)

    monkeypatch.setattr(store, "connection", interleaved)
    result = store.metric_history("BTC", "funding", as_of=NOW)
    assert inserted
    assert [row["value"] for row in result["points"]] == [1]
    assert result["provenance"]["observation_watermark"] == 1


def test_cursor_rejects_selection_store_and_retention_changes(store, tmp_path):
    store.put_observations([point(NOW - 20 * 86400 - offset, metric="spread_bps") for offset in (1, 2, 3)])
    first = store.metric_history("BTC", "spread_bps", as_of=NOW, limit=1)
    cursor = first["next_cursor"]
    assert cursor
    with pytest.raises(ValueError, match="selection or store"):
        store.metric_history("BTC", "spread_bps", source="other", as_of=NOW, cursor=cursor)
    with pytest.raises(ValueError, match="selection or store"):
        Store(tmp_path / "other.sqlite").metric_history("BTC", "spread_bps", as_of=NOW, cursor=cursor)
    compact_liquidity(store, now=NOW)
    with pytest.raises(ValueError, match="Stale"):
        store.metric_history("BTC", "spread_bps", as_of=NOW, cursor=cursor)


@pytest.mark.parametrize("selectors", [dict(limit=True), dict(limit=501), dict(as_of=True),
                                      dict(start=NOW, end=NOW - 1), dict(end=NOW + 1),
                                      dict(source=""), dict(quote=1), dict(cursor="broken"),
                                      dict(dimension={"instrument": "swap"}),
                                      dict(dimension={"quote": "EUR", "unit": "percent"}, quote="USD")])
def test_invalid_selectors_fail_explicitly(store, selectors):
    with pytest.raises(ValueError):
        store.metric_history("BTC", "funding", **{"as_of": NOW, **selectors})


def test_time_bounds_are_inclusive_and_late_versions_remain_unknown(store):
    store.put_observations([point(NOW - 2), point(NOW - 1), point(),
                            point(NOW - 1, value=999, observed_at=NOW + 1)])
    result = store.metric_history("BTC", "funding", as_of=NOW, start=NOW - 2, end=NOW - 1)
    assert [row["event_time"] for row in result["points"]] == [NOW - 1, NOW - 2]
    assert all(row["value"] == 1 for row in result["points"])


def test_actual_mcp_history_keeps_legacy_fields_and_adds_native_pagination(tmp_path, monkeypatch):
    store = Store(tmp_path / "observer.sqlite")
    store.put_observations([point(NOW - offset) for offset in range(4)])
    store.put_observations([point(NOW, source="other", quote="EUR", metric="noise")])
    monkeypatch.setattr(Store, "observations", lambda *args, **kwargs: pytest.fail("global history read"))
    server = create_server(Settings(data_dir=tmp_path))

    async def check():
        args = dict(asset="BTC", metric="funding", source="venue", quote="USD", as_of=NOW, limit=2)
        _, first = await server.call_tool("get_metric_history", args)
        assert {"points", "truncated", "asset", "metric", "data_mode"} <= first.keys()
        assert first["truncated"] is True
        _, second = await server.call_tool("get_metric_history", {**args, "cursor": first["next_cursor"]})
        assert len(first["points"] + second["points"]) == 4
        assert second["truncated"] is False

    asyncio.run(check())


def test_real_sdk_verbose_points_continue_from_last_emitted_record(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    store.put_observations([point(NOW - offset, value=offset,
                                  metadata={"instrument": "perpetual", "details": "fixture " * 160})
                            for offset in range(23)])
    server = create_server(Settings(data_dir=tmp_path))

    async def check():
        args = dict(asset="BTC", metric="funding", as_of=NOW, limit=100)
        values = []
        async with create_connected_server_and_client_session(server) as session:
            while True:
                response = await session.call_tool("get_metric_history", args)
                assert not response.isError
                assert len(response.model_dump_json().encode()) < 32768
                page = response.structuredContent
                assert isinstance(page["points"], list)
                assert page["points"]
                values.extend(row["value"] for row in page["points"])
                if page["next_cursor"] is None:
                    assert page["truncated"] is False
                    break
                assert page["truncated"] is True
                assert "response_byte_budget" in page["truncation_reasons"]
                args["cursor"] = page["next_cursor"]
        assert values == list(range(23))

    asyncio.run(check())


def test_single_oversize_point_fails_without_advancing(store):
    store.put_observations([point(metadata={"details": "fixture " * 2000})])
    with pytest.raises(ValueError, match="cannot emit or advance"):
        store.metric_history("BTC", "funding", as_of=NOW)


def test_real_stdio_sparse_native_series_beyond_former_200k_cap(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    store.put_observations([point(NOW - 300000 - offset, value=offset) for offset in range(3)])
    for start in range(0, 200001, 5000):
        store.put_observations([point(NOW - offset, source="other", quote="EUR")
                                for offset in range(start, min(start + 5000, 200001))])
    args = dict(asset="BTC", metric="funding", source="venue", quote="USD", as_of=NOW, limit=2)
    assert len(store.metric_history(**args)["points"]) == 2

    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--data-dir", str(tmp_path)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            first = await session.call_tool("get_metric_history", args)
            assert not first.isError
            assert first.structuredContent["truncated"] is True
            assert first.structuredContent["remaining_points"] == 1
            second = await session.call_tool("get_metric_history", {
                **args, "cursor": first.structuredContent["next_cursor"]})
            assert not second.isError
            assert second.structuredContent["truncated"] is False
            assert [row["value"] for row in first.structuredContent["points"]
                    + second.structuredContent["points"]] == [0, 1, 2]

    asyncio.run(check())
