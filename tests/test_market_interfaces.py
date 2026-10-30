"""Real local CLI/MCP paths for synthetic resolved instruments, without HTTP."""

import asyncio
import hashlib
import io
import json
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.cross_asset.application import validate_bundle
from crypto_market_analysis.cross_asset.storage import CrossAssetStore
from crypto_market_analysis.request_schemas import get_request_schema
from crypto_market_analysis.server import create_server
from tools.benchmark_cross_asset import START, synthetic_bundle

END = START + 10 * 86400
TOOLS = {"get_market_capabilities", "get_instrument_registry", "get_market_calendar", "get_instrument_analysis",
         "simulate_instrument_plan", "get_instrument_series", "get_market_snapshot_status", "get_market_fleet",
         "get_instrument_outcome", "calculate_futures_value", "build_futures_research_series"}


def invoke(command, request, directory, monkeypatch, capsys, *, extra=(), expected=0):
    args = [command, "--data-dir", str(directory), *extra]
    if request is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
        args.extend(["--request", "-"])
    assert main(args) == expected
    captured = capsys.readouterr()
    return json.loads(captured.out) if captured.out else captured.err


def analysis_request():
    return {"series_id": "synthetic:series:0", "data_mode": "demo", "currency": "USD", "start": START, "end": END,
            "annualization": {"periods_per_year": 252, "convention": "synthetic declared daily sessions"}}


def plan_request(bundle):
    request = analysis_request()
    request.pop("annualization")
    request["plan"] = {"currency": "USD", "initial_cash": 1000, "deposits": [],
                       "purchases": [{"at": bundle["series"][0]["bars"][0]["end"], "cash_amount": 500}],
                       "fee_per_trade": 1, "distributions": "cash", "actions_complete": False}
    return request


def test_explicit_import_and_native_plan_retain_original_fill_vintage(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "absent"
    bundle = synthetic_bundle(5)
    preview = invoke("market-import", bundle, directory, monkeypatch, capsys)
    assert preview["status"] == "validated" and preview["applied"] is False and not directory.exists()
    applied = invoke("market-import", bundle, directory, monkeypatch, capsys, extra=("--apply",))
    assert applied["applied"] is True
    clock = ("--as-of", str(END))
    result = invoke("market-plan", plan_request(bundle), directory, monkeypatch, capsys, extra=clock)
    assert result["status"] == "ok", result
    assert result["plan"]["fills"][0]["price"] == 100
    assert result["plan"]["mark"]["price"] == 109.1
    assert result["plan"]["cash"] == 499
    assert result["plan"]["evidence"] != "independently_verified_point_in_time"
    request = analysis_request()
    request["currency"] = "EUR"
    wrong = invoke("market-analysis", request, directory, monkeypatch, capsys, extra=clock, expected=1)
    assert wrong["status"] == "invalid_request" and "currency" in wrong["reason"]
    registry = invoke("market-registry", {"provider": "synthetic", "symbol": "EXAMPLE0", "effective_at": START},
                      directory, monkeypatch, capsys, extra=clock)
    assert registry["registry"]["instrument"]["asset_class"] == "equity"


def test_readonly_commands_and_unknown_requests_do_not_create_store(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "missing"
    for command, request in (("market-capabilities", None), ("market-registry", {}), ("market-snapshot", None),
                             ("market-series", {"series_id": "missing", "data_mode": "demo"})):
        result = invoke(command, request, directory, monkeypatch, capsys)
        assert result.get("status") in {"experimental", "unavailable", "missing"}
    assert not directory.exists()
    rejected = invoke("market-import", synthetic_bundle(), directory, monkeypatch, capsys,
                      extra=("--apply", "--as-of", str(END)), expected=2)
    assert "actual" in rejected and not directory.exists()


def test_fetch_preflight_and_apply_resolve_metadata_without_inventing_observations(tmp_path, monkeypatch, capsys):
    bundle = synthetic_bundle(1, vintages=1)
    series = bundle["series"][0]
    series["descriptor"].update(provider="normalized_import", data_mode="import", revision_semantics="captured_history")
    bundle["registry"]["aliases"][0]["provider"] = "normalized_import"
    request = {"registry": bundle["registry"], "calendars": bundle["calendars"], "source_request": {
        "descriptor": series["descriptor"], "start": START, "end": END,
        "source_contract": {"origin": "Invented local fixture", "rights": "Synthetic fixture; no market feed",
                            "price_basis": "raw"}, "import_data": {"bars": series["bars"], "actions": []}}}
    directory = tmp_path / "capture"
    preview = invoke("market-fetch", request, directory, monkeypatch, capsys)
    assert preview["applied"] is False and not directory.exists()
    applied = invoke("market-fetch", request, directory, monkeypatch, capsys, extra=("--apply",))
    assert applied["applied"] is True and applied["source_assessment"]["production_accepted"] is False
    stored = CrossAssetStore(directory / "observer.sqlite").read_series("synthetic:series:0",
        as_of=applied["receipt"]["received_at"], data_mode="import")
    assert len(stored["bars"]) == 8 and stored["descriptor"]["provider"] == "normalized_import"


def test_snapshot_fleet_preserves_negative_references_and_never_mutates_authority(tmp_path, monkeypatch, capsys):
    store = CrossAssetStore(tmp_path / "observer.sqlite", initialize=True)
    store.import_bundle(validate_bundle(synthetic_bundle()), received_at=END)
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    result = invoke("market-snapshot", {}, tmp_path, monkeypatch, capsys, extra=("--apply", "--name", "research"))
    assert result["status"] == "exported"
    request = {"series_ids": [f"synthetic:series:{i}" for i in range(5)], "data_mode": "demo", "start": START, "end": END}
    result = invoke("market-fleet", request, tmp_path, monkeypatch, capsys,
                    extra=("--name", "research", "--as-of", str(END)))
    reference = next(row for row in result["series"] if row["series_id"] == "synthetic:series:2")
    assert reference["simple_return"] is None and reference["last_close"] < 0
    assert reference["descriptor"]["listing_id"] is None
    assert result["total_rows"] == 42 and result["integrity_verified"]
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


def test_all_market_tools_real_mcp_are_readonly_discoverable_and_synthetic(tmp_path):
    store = CrossAssetStore(tmp_path / "observer.sqlite", initialize=True)
    store.import_bundle(validate_bundle(synthetic_bundle()), received_at=END)
    server = create_server(Settings(data_dir=tmp_path), "demo")
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(listed) == 64 and TOOLS <= listed.keys()
            assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in listed.values())
            for name in TOOLS:
                assert get_request_schema(name)["tool_name"] == name
            requests = {
                "get_market_capabilities": {},
                "get_instrument_registry": {"request": {"effective_at": START}, "as_of": END},
                "get_market_calendar": {"request": {"calendar_id": "synthetic:publication", "calendar_version": "synthetic-v1",
                                                    "start": START, "end": END, "interval": "reference"}, "as_of": END},
                "get_instrument_analysis": {"request": analysis_request(), "as_of": END},
                "simulate_instrument_plan": {"request": plan_request(synthetic_bundle()), "as_of": END},
                "get_instrument_series": {"request": {"series_id": "synthetic:series:0", "data_mode": "demo",
                                                       "all_vintages": True}, "as_of": END},
                "get_market_snapshot_status": {},
                "calculate_futures_value": get_request_schema("calculate_futures_value")["examples"][0],
            }
            for name, arguments in requests.items():
                result = await session.call_tool(name, arguments)
                assert not result.isError, (name, result)
            curve = json.loads((Path(__file__).resolve().parents[1] / "examples/market-futures-curve.json").read_text())
            result = await session.call_tool("build_futures_research_series", {"request": curve, "as_of": 30})
            assert not result.isError and result.structuredContent["executable"] is False
    asyncio.run(check())
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


def test_fresh_native_store_backup_restores_exact_evidence(tmp_path):
    from crypto_market_analysis.recovery import create_backup, restore_backup, verify_backup

    store = CrossAssetStore(tmp_path / "original" / "observer.sqlite", initialize=True)
    store.import_bundle(validate_bundle(synthetic_bundle()), received_at=END)
    create_backup(store.path, tmp_path / "backup")
    verified = verify_backup(tmp_path / "backup")
    assert verified["status"] == "verified"
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    restored = CrossAssetStore(tmp_path / "restored" / "observer.sqlite")
    assert restored.read_series("synthetic:series:2", as_of=END, data_mode="demo", all_vintages=True) == store.read_series(
        "synthetic:series:2", as_of=END, data_mode="demo", all_vintages=True)
