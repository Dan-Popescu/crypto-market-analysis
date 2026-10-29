"""The offline harness must fail closed and report failures to its caller."""

import importlib.util
import json
import socket
import subprocess
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
