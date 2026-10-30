"""Public scalable workflows retain declared scope, immutable artifacts and read-only MCP."""

import asyncio
import json
import sys
from copy import deepcopy
from io import StringIO

import jsonschema
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from crypto_market_analysis.analytical_store import export_candle_snapshot
from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle
from crypto_market_analysis.request_schemas import _VALIDATION_EXAMPLE, AS_OF, get_request_schema
from crypto_market_analysis.server import create_server
from crypto_market_analysis.storage import Store
from crypto_market_analysis.validation_protocol import freeze_validation_protocol

TOOLS = {"get_collection_capacity", "get_operating_health", "get_operating_costs", "get_operating_acceptance",
         "get_prospective_validation", "get_analytical_snapshot_status", "get_analytical_fleet"}


def invoke(args, request, monkeypatch, capsys, expected=0):
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request)))
    assert main([*args, "--request", "-"]) == expected
    output = capsys.readouterr()
    return json.loads(output.out) if expected == 0 and output.out else output.err


def test_thousand_asset_capacity_and_missing_operating_reads_do_not_initialize(tmp_path, monkeypatch, capsys):
    absent = tmp_path / "absent"
    config = tmp_path / "universe.toml"
    config.write_text("assets = " + json.dumps([f"ASSET{i}" for i in range(1000)]) + '\nvenues=["kraken"]\n')
    result = invoke(["collection-capacity", "--config", str(config), "--data-dir", str(absent)],
                    {"tasks": ["market"]}, monkeypatch, capsys)
    assert result["assets"] == 1000 and result["market_work_units"] == 4000
    assert result["actual_http_requests"] is None
    for command in ("operating-health", "operating-costs", "operating-acceptance", "analytics-snapshot"):
        result = invoke([command, "--data-dir", str(absent)], {}, monkeypatch, capsys)
        assert result["status"] in ("missing", "not_recorded", "database_unavailable")
        assert not absent.exists()


def test_protocol_freeze_is_actual_clock_canonical_idempotent_and_immutable(tmp_path, monkeypatch, capsys):
    declaration = deepcopy(_VALIDATION_EXAMPLE)
    declaration["baseline"]["fees"]["rate"] = "0.0"
    output = tmp_path / "frozen.json"
    args = ["freeze-validation", "--output", str(output), "--data-dir", str(tmp_path / "absent")]
    invoke(args, declaration, monkeypatch, capsys)
    first = output.read_bytes()
    artifact = json.loads(first)
    assert artifact["frozen_at"] > AS_OF and artifact["declaration"]["baseline"]["fees"]["rate"] == "0"
    invoke(args, declaration, monkeypatch, capsys)
    assert output.read_bytes() == first
    changed = deepcopy(declaration)
    changed["criterion"]["minimum_resolved_nonoverlapping"] = 3
    assert "cannot be replaced" in invoke(args, changed, monkeypatch, capsys, expected=2)
    assert output.read_bytes() == first and not (tmp_path / "absent").exists()


def test_issue_binds_actual_supported_predictions_protocol_and_retry_identity(tmp_path, monkeypatch):
    now = 1000 * 86400
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: now)
    engine = Engine(Settings(data_dir=tmp_path), "demo")
    engine.store.put_candles([Candle("synthetic", "SOL", "EUR", "1h", opened, opened+3600,
        100, 101, 99, 100, 1, opened+3600, data_mode="demo")
        for opened in range(now-100*86400, now, 3600)])
    declaration = deepcopy(_VALIDATION_EXAMPLE)
    declaration.update(mode="prospective", starts_at=now, ends_at=now+86400)
    cohort = declaration["cohorts"][0]
    cohort.update(asset="SOL", rules={"context": 1, "purchase_windows": 1, "outcome_protocol": 1})
    protocol = freeze_validation_protocol(declaration, frozen_at=now-1)
    original = engine.issue_assessment("bound", asset="SOL", source="synthetic", quote="EUR", validation_protocol=protocol)
    assert original["payload"]["validation_protocol_hash"] == protocol["protocol_hash"]
    predictions = original["payload"]["validation_predictions"]
    assert len(predictions) == 1 and predictions[0]["window_start"] == now
    assert predictions[0]["probability"] == 0
    assert engine.issue_assessment("bound", asset="SOL", source="synthetic", quote="EUR", validation_protocol=protocol) == original
    changed = deepcopy(declaration)
    changed["criterion"]["minimum_resolved_nonoverlapping"] = 3
    with pytest.raises(ValueError):
        engine.issue_assessment("bound", asset="SOL", source="synthetic", quote="EUR",
                                validation_protocol=freeze_validation_protocol(changed, frozen_at=now-1))
    monkeypatch.setattr("crypto_market_analysis.application.time.time", lambda: now+86400)
    with pytest.raises(ValueError, match="outside"):
        engine.issue_assessment("end", asset="SOL", source="synthetic", quote="EUR", validation_protocol=protocol)


def test_registered_scalability_examples_and_thousand_asset_fleet_are_read_only(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    assets = [f"ASSET{i}" for i in range(1000)]
    store.put_candles([Candle("synthetic", asset, "EUR", "1h", AS_OF-3600, AS_OF,
                              index+1, index+1, index+1, index+1, 1, AS_OF, data_mode="demo")
                       for index, asset in enumerate(assets)])
    export_candle_snapshot(store, tmp_path / "analytical_snapshots" / "research")
    server = create_server(Settings(data_dir=tmp_path), "demo")
    before = (tmp_path / "observer.sqlite").read_bytes()

    async def check():
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert len(tools) == 53 and TOOLS <= tools.keys()
        assert all(tool.annotations.readOnlyHint for tool in tools.values())
        for name in TOOLS:
            contract = get_request_schema(name)
            for example in contract["examples"]:
                jsonschema.validate(example, contract["input_schema"])
                result = await server.call_tool(name, example)
                assert not result.isError, (name, result)
                assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
        fleet = await server.call_tool("get_analytical_fleet", {"snapshot_name": "research", "request": {
            "assets": assets, "source": "synthetic", "quote": "EUR", "mode": "demo",
            "start": AS_OF-3600, "end": AS_OF, "as_of": AS_OF}})
        assert not fleet.isError and len(fleet.model_dump_json().encode()) <= 32768
        assert fleet.structuredContent["selected_points"] == 1000
        with pytest.raises(ToolError, match="read-only"):
            await server.call_tool("get_operating_acceptance", {"request": {"refresh": True}})
        with pytest.raises(ToolError, match="Snapshot name"):
            await server.call_tool("get_analytical_snapshot_status", {"snapshot_name": "../escape"})

    asyncio.run(check())
    assert (tmp_path / "observer.sqlite").read_bytes() == before


def test_operating_session_preserves_unrelated_staging_files_and_rejects_symlinks(tmp_path, monkeypatch):
    from crypto_market_analysis import operating_acceptance

    settings = Settings(data_dir=tmp_path / "data", venues=["kraken"], assets=["BTC"], quote_currencies=["EUR"])
    directory = settings.data_dir / "operating_acceptance" / "bounded"
    directory.mkdir(parents=True)
    unrelated = directory / "manifest.tmp"
    unrelated.write_text("unrelated existing content")
    monkeypatch.setattr(operating_acceptance, "collect", lambda *a, **k: {"status": "empty", "reports": []})
    result = operating_acceptance.run_operating_acceptance(settings,
        {"session_id": "bounded", "refresh": True, "max_cycles": 1, "max_duration_seconds": 2}, clients=object())
    assert result["completed_this_invocation"] == 1
    assert unrelated.read_text() == "unrelated existing content"
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory.parent / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        operating_acceptance.run_operating_acceptance(settings, {"session_id": "linked", "refresh": True}, clients=object())
    assert list(outside.iterdir()) == []
