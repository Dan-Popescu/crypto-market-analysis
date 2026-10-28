import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from crypto_market_analysis.config import Settings
from crypto_market_analysis.server import create_server


def test_stdio_real_protocol_and_read_only_annotations(tmp_path):
    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--data-dir", str(tmp_path)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            initialization = await session.initialize()
            assert "Analyze BTC/ETH over 1–60 days." in initialization.instructions
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {"get_analysis_bundle", "get_source_status", "render_chart", "get_forecast"} <= names
            assert all(t.annotations.readOnlyHint for t in listed.tools)
            result = await session.call_tool("get_analysis_bundle", {"asset": "BTC"})
            assert not result.isError
            assert result.structuredContent["selected_market"] is None
            invalid = await session.call_tool("get_forecast", {"horizon": 61})
            assert invalid.isError
            assert "Horizon must be between 1 and 60 days." in invalid.content[0].text
            glossary = await session.call_tool("explain_metric", {"metric": "depth"})
            assert glossary.structuredContent["limit"] == (
                "Liquidity can disappear. Incomplete coverage gives a lower bound."
            )

    asyncio.run(check())


def test_http_real_protocol(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "crypto_market_analysis",
            "serve",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--data-dir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        with httpx.Client(trust_env=False, timeout=0.3) as client:
            for _ in range(80):
                if process.poll() is not None:
                    pytest.fail("The MCP HTTP server exited prematurely.")
                try:
                    client.get(url)
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                pytest.fail("The MCP HTTP server is not responding.")

        async def check():
            async with httpx.AsyncClient(trust_env=False) as client:
                async with (
                    streamable_http_client(url, http_client=client) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool("get_source_status", {})
                    assert not result.isError
                    assert result.structuredContent["paid_collection_apis"] is False

        asyncio.run(check())
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_public_listen_requires_oauth(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("OBSERVER_"):
            monkeypatch.delenv(key)
    with pytest.raises(ValueError, match="OAuth"):
        create_server(Settings(data_dir=tmp_path), host="0.0.0.0")


def test_jwt_audience_issuer_and_scope_are_verified(monkeypatch):
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from crypto_market_analysis.server import JWTVerifier

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = JWTVerifier(
        "https://issuer.example", "https://issuer.example/keys", "observer", "https://observer.example/mcp"
    )
    monkeypatch.setattr(
        verifier.keys, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key=key.public_key())
    )
    claims = {
        "iss": "https://issuer.example",
        "aud": "observer",
        "sub": "user",
        "scope": "observer:read",
        "exp": int(time.time()) + 100,
    }
    valid = jwt.encode(claims, key, algorithm="RS256")
    result = asyncio.run(verifier.verify_token(valid))
    assert result.scopes == ["observer:read"]
    invalid = jwt.encode({**claims, "aud": "another-service"}, key, algorithm="RS256")
    assert asyncio.run(verifier.verify_token(invalid)) is None


def test_mcp_chart_returns_native_image_content(tmp_path):
    import base64

    from crypto_market_analysis.demo import seed_demo
    from crypto_market_analysis.storage import Store

    seed_demo(Store(tmp_path / "observer.sqlite"), days=90)

    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "crypto_market_analysis", "serve", "--mode", "demo", "--data-dir", str(tmp_path)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("render_chart", {"asset": "BTC", "kind": "weekly"})
            assert not result.isError
            images = [c for c in result.content if c.type == "image"]
            assert len(images) == 1
            assert base64.b64decode(images[0].data).startswith(b"\x89PNG")

    asyncio.run(check())
