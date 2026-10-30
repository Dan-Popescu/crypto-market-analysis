"""Dossier presentation retains every native market and pages exact details."""
import asyncio
import hashlib
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from crypto_market_analysis.analysis_dossier import build_analysis_dossier
from crypto_market_analysis.config import Settings
from crypto_market_analysis.mcp_results import RESPONSE_BYTE_LIMIT, canonical_json
from crypto_market_analysis.models import Candle
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store

NOW = 10 * 86400


@pytest.mark.parametrize("lookbacks", [[7], None], ids=["one-lookback", "default-lookbacks"])
def test_four_market_core_and_lossless_frozen_dossier_paging(tmp_path, lookbacks):
    settings = Settings(data_dir=tmp_path)
    store = Store(settings.database)
    markets = [{"asset": asset, "source": "synthetic", "quote": quote,
                **({"lookbacks": lookbacks} if lookbacks is not None else {})}
               for asset in ("BTC", "ETH") for quote in ("EUR", "USD")]
    store.put_candles([Candle(m["source"], m["asset"], m["quote"], "1h", time, time + 3600,
                             price, price, price, price, 1, time + 3600, data_mode="demo")
                      for m, price in zip(markets, (100, 200, 10, 20), strict=True)
                      for time in range(NOW - 8 * 86400, NOW, 3600)])
    request = {"data_mode": "demo", "markets": markets}
    expected = build_analysis_dossier(store, request, NOW)
    server = create_server(settings, "demo")

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            response = await session.call_tool("get_analysis_dossier", {"request": request, "as_of": NOW})
            assert not response.isError
            assert len(response.model_dump_json(by_alias=True, exclude_none=True).encode()) <= RESPONSE_BYTE_LIMIT
            compact = response.structuredContent
            assert isinstance(compact["markets"], list) and len(compact["markets"]) == 4
            for actual, original in zip(compact["markets"], expected["markets"], strict=True):
                assert actual["selection"] == original["selection"]
                context = actual["market_context"]
                for key in ("status", "anchor", "market", "as_of", "coverage"):
                    assert context[key] == original["market_context"][key]
                for section in ("venue_disagreement", "venue_costs", "derivatives", "quotes"):
                    assert actual[section]["status"] == original[section]["status"] == "not_requested"
            assert compact["as_of"] == NOW
            assert compact["watermark"] == expected["watermark"]
            metadata = compact["_mcp_result"]
            assert "/markets" not in metadata["omitted_paths"]
            assert metadata["content_hash"] == hashlib.sha256(canonical_json(expected)).hexdigest()
            binding = {key: metadata[key] for key in ("handle", "selection_hash", "content_hash")}
            restored, offset = {}, 0
            database_before_pages = settings.database.read_bytes()
            while offset is not None:
                page_response = await session.call_tool("get_result_page", {**binding, "offset": offset})
                assert not page_response.isError
                assert len(page_response.model_dump_json(by_alias=True, exclude_none=True).encode()) <= RESPONSE_BYTE_LIMIT
                page = page_response.structuredContent
                if "items" in page:
                    restored.update((entry["key"], entry["value"]) for entry in page["items"])
                else:
                    fragments = [page["fragment"]]
                    while page["next_fragment_offset"] is not None:
                        page_response = await session.call_tool("get_result_page", {**binding, "offset": offset,
                            "fragment_offset": page["next_fragment_offset"]})
                        assert not page_response.isError
                        assert len(page_response.model_dump_json(by_alias=True, exclude_none=True).encode()) <= RESPONSE_BYTE_LIMIT
                        page = page_response.structuredContent
                        fragments.append(page["fragment"])
                    entry = json.loads("".join(fragments))
                    restored[entry["key"]] = entry["value"]
                offset = page["next_offset"]
            assert restored == expected
            assert hashlib.sha256(canonical_json(restored)).hexdigest() == metadata["content_hash"]
            assert settings.database.read_bytes() == database_before_pages
            # Unavailable and undefined statistics retain their actual nulls in
            # the frozen source, even when full lookback details require paging.
            position = restored["markets"][0]["market_context"]["lookbacks"]["7"]["range_position"]
            assert position["value"] is None and position["status"] == "undefined"
    asyncio.run(check())
