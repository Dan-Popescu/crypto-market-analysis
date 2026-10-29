"""The offline harness must fail closed and report failures to its caller."""

import importlib.util
import json
import socket
import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "benchmark_endurance.py"
SPEC = importlib.util.spec_from_file_location("benchmark_endurance", SCRIPT)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


def test_network_guard_blocks_dns_stream_and_datagram_and_restores():
    original = socket.create_connection
    with HARNESS.block_network() as attempts:
        with pytest.raises(RuntimeError, match="Network disabled"):
            socket.create_connection(("example.invalid", 80))
        with pytest.raises(RuntimeError, match="Network disabled"):
            socket.getaddrinfo("example.invalid", 80)
        # Exercise the guarded boundary without creating an OS socket: some
        # managed test environments forbid even unconnected socket allocation.
        with pytest.raises(RuntimeError, match="Network disabled"):
            socket.socket.sendto(None, b"synthetic", ("127.0.0.1", 9))
        assert len(attempts) == 3
    assert socket.create_connection is original


@pytest.mark.parametrize("arguments", [
    ["--virtual-days", "0"], ["--virtual-days", "31"], ["--cycles-per-day", "289"],
    ["--max-cycles", "3"], ["--max-cycles", "2049"], ["--readers", "9"],
    ["--reads-per-cycle", "9"], ["--timeout", "301"], ["--slow-ms", "21"],
])
def test_configuration_rejects_unbounded_work(arguments):
    with pytest.raises(SystemExit) as exc:
        HARNESS.parser().parse_args(arguments)
    assert exc.value.code == 2


def test_worker_assertion_failure_returns_nonzero_and_preserves_report(tmp_path, monkeypatch):
    destination = tmp_path / "failure.json"
    monkeypatch.setattr(HARNESS.subprocess, "check_output", lambda *a, **kw: "fixture-revision")
    monkeypatch.setattr(HARNESS.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, "", "AssertionError: native quote mismatch"))
    assert HARNESS.main(["--output", str(destination)]) == 1
    report = json.loads(destination.read_text())
    assert report["status"] == "failed"
    assert "native quote mismatch" in report["correctness_mismatches"][0]


def test_correctness_checks_survive_optimized_python():
    invocation = [HARNESS.sys.executable, "-O", "-c",
                  f"import runpy; runpy.run_path({str(SCRIPT)!r})['require'](False, 'synthetic mismatch')"]
    result = subprocess.run(invocation, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "synthetic mismatch" in result.stderr


def test_reader_mismatch_survives_scheduler_exception_boundary(tmp_path, monkeypatch):
    # Production scheduling catches collector exceptions and records a failed
    # attempt. A harness oracle failure must still stop the experiment itself.
    async def corrupted_result(*args, **kwargs):
        raise AssertionError("injected frozen-page mismatch")

    monkeypatch.setattr(HARNESS, "full_result", corrupted_result)
    arguments = HARNESS.parser().parse_args(["--phase", "1", "--data-dir", str(tmp_path),
                                            "--virtual-days", "1", "--cycles-per-day", "4", "--readers", "1"])
    with HARNESS.block_network(), pytest.raises(AssertionError, match="injected frozen-page mismatch"):
        HARNESS.run_phase(tmp_path, arguments)


def test_finite_pipeline_restarts_and_checks_native_values(tmp_path):
    destination = tmp_path / "endurance.json"
    assert HARNESS.main(["--virtual-days", "1", "--cycles-per-day", "4", "--readers", "1",
                         "--output", str(destination)]) == 0
    report = json.loads(destination.read_text())
    assert report["status"] == "passed"
    assert report["actual_http_requests"] == 0
    assert report["phases"][1]["restart"]["previous_pid"] != report["phases"][1]["pid"]
    assert all(p["native_candle_oracle_rows"] > 0 for p in report["phases"])
    assert all(p["frozen_history_page_calls"] > 0 for p in report["phases"])
    assert all(p["lifecycle"]["archive_quota_preserved_hot_evidence"] for p in report["phases"])
    assert all(p["integrated_mcp_preserved_database"] for p in report["phases"])
    assert all(p["auto_repair_status"] == "not_configured" for p in report["phases"])
    assert all(any(s["candle_rows"] > 0 and s["shared_section_watermarks"] for s in p["dossier_samples"])
               for p in report["phases"])
    assert all(s["totals"] is None for p in report["phases"] for s in p["stored_http_accounting_samples"])
    assert all(any(s["coverage"] == "uninstrumented" for s in p["stored_http_accounting_samples"])
               for p in report["phases"])


def test_virtual_days_do_not_advance_runtime_resource_deadlines():
    clock = HARNESS.Clock(1000)
    runtime = HARNESS.WallClock(clock)
    clock.now += 7 * 86400
    assert runtime.time() == clock.now
    assert runtime.monotonic is time.monotonic
    assert clock.monotonic() == clock.now  # Catalogue/cache TTLs intentionally advance.


def test_dossier_oracle_rejects_split_watermarks_and_changed_native_price(tmp_path):
    from crypto_market_analysis.analysis_dossier import build_analysis_dossier
    from crypto_market_analysis.models import Candle

    store = HARNESS.demo_store(tmp_path / "observer.sqlite")
    now = 1780272000
    store.put_candles([Candle("kraken", "BTC", "EUR", "1h", now - 3600, now,
                              100, 102, 99, 101, 2, now, data_mode="demo")])
    wanted = {"data_mode": "demo", "markets": [{"asset": "BTC", "source": "kraken", "quote": "EUR",
              "lookbacks": [1, 7], "venues": {"sources": ["kraken"], "purchase_costs": {"budget": "100"}},
              "derivatives": {"source": "synthetic-oracle", "quote": "EUR"},
              "quotes": {"sources": ["synthetic-oracle"]}}]}
    dossier = build_analysis_dossier(store, wanted, now)
    assert HARNESS.validate_dossier(dossier, "EUR", now) == 1
    split = deepcopy(dossier)
    split["calendar"]["stored_inputs"]["watermark"]["candles"] += 1
    with pytest.raises(AssertionError, match="one physical watermark"):
        HARNESS.validate_dossier(split, "EUR", now)
    changed = deepcopy(dossier)
    changed["markets"][0]["market_context"]["anchor"]["price"] = 201
    with pytest.raises(AssertionError, match="positive native candle price"):
        HARNESS.validate_dossier(changed, "EUR", now)


def test_http_oracle_preserves_unknown_instrumentation_instead_of_zero(tmp_path):
    from crypto_market_analysis.http_accounting import stored_http_accounting
    from crypto_market_analysis.models import CollectionResult

    store = HARNESS.demo_store(tmp_path / "observer.sqlite")
    store.record_run(CollectionResult("kraken"), "market", 100)
    accounting = stored_http_accounting(store.path, source="kraken", task="market", mode="demo", end=100,
                                        limit=128, endpoint_limit=16)
    HARNESS.validate_http_accounting(accounting)
    assert accounting["summary"]["coverage"] == "uninstrumented"
    accounting["summary"]["totals"] = {"requests": 0}
    with pytest.raises(AssertionError, match="falsely reported"):
        HARNESS.validate_http_accounting(accounting)
