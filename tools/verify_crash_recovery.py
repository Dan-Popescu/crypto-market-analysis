"""Run bounded offline SIGKILL regressions and write evidence outside Git.

PYTHONPATH=src python tools/verify_crash_recovery.py --output /tmp/crash-recovery.json
Requires the development pytest dependency and POSIX process groups. This checks
process death and SQLite rollback, not power loss or filesystem fault tolerance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

SCENARIOS = {
    "test_sigkill_pending_capture_read_only_then_exact_idempotent_recovery": {
        "boundary": "immutable payloads and pending journal visible before candle materialization",
        "invariants": ["read-only planning/status leave journal and rows unchanged",
                       "explicit reopen preserves original receipt, normalized and raw hashes",
                       "second reopen does not duplicate capture or candles"],
    },
    "test_sigkill_materialized_capture_reconciles_only_actual_missing_hours[complete]": {
        "boundary": "capture committed while automatic task checkpoint still inflight",
        "invariants": ["fully covered task completes without another fetch",
                       "original capture and rows preserved", "second apply performs no requests"],
    },
    "test_sigkill_materialized_capture_reconciles_only_actual_missing_hours[partial]": {
        "boundary": "partial capture committed while automatic task checkpoint still inflight",
        "invariants": ["retry requests only the actual missing hour",
                       "original capture and rows preserved", "second apply performs no requests"],
    },
    "test_sigkill_archive_restart_preserves_exact_evidence_and_rolls_back_delete[archive_pending]": {
        "boundary": "complete archive staging file verified before rename",
        "invariants": ["hot originals survive process death", "retry publishes identical archive bytes",
                       "only verified archived rows pruned", "second maintenance is idle"],
    },
    "test_sigkill_archive_restart_preserves_exact_evidence_and_rolls_back_delete[archive_published]": {
        "boundary": "verified archive published before pruning begins",
        "invariants": ["hot originals survive process death", "retry reuses identical archive bytes",
                       "only verified archived rows pruned", "second maintenance is idle"],
    },
    "test_sigkill_archive_restart_preserves_exact_evidence_and_rolls_back_delete[archive_uncommitted_delete]": {
        "boundary": "hot row DELETE executed inside uncommitted SQLite transaction",
        "invariants": ["independent reader sees committed original before SIGKILL",
                       "killed writer transaction rolls back", "retry preserves exact archived evidence"],
    },
    "test_sigkill_retention_snapshot_before_pruning_does_not_recompute_median": {
        "boundary": "raw evidence archived and hourly aggregate committed before raw pruning",
        "invariants": ["retry preserves original median and sample count without new aggregate",
                       "raw rows pruned only after exact archive exists", "second compaction is idle"],
    },
}


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, timeout=5).stdout.strip()


def _outside_git(path):
    existing = path.parent
    while not existing.exists():
        existing = existing.parent
    check = subprocess.run(["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
                           capture_output=True, check=False, timeout=5)
    if check.returncode == 0:
        raise ValueError("Generated crash evidence must be written outside any Git working tree")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deadline-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("This experiment requires POSIX SIGKILL and process groups")
    if not 10 <= args.deadline_seconds <= 600:
        parser.error("--deadline-seconds must be between 10 and 600")
    output = args.output.expanduser().resolve()
    _outside_git(output)
    root = Path(__file__).resolve().parents[1]
    files = ["tests/test_process_crash_recovery.py", "tools/verify_crash_recovery.py",
             *[f"src/crypto_market_analysis/{name}.py" for name in
               ("history_dataset", "automatic_repair", "retention", "storage_policy", "storage")]]
    evidence = {
        "schema_version": 1, "experiment": "offline synthetic process-crash recovery",
        "started_at": datetime.now(UTC).isoformat(), "source_revision": _git(root, "rev-parse", "HEAD"),
        "source_worktree_changes": _git(root, "status", "--porcelain"),
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
        "scope": "SIGKILL of real child processes at parent-observed persisted file/SQLite boundaries",
        "limitations": ["not a power-loss or filesystem-corruption experiment",
                        "small synthetic fixtures do not establish production throughput or provider acceptance"],
        "financial_evidence": "injected synthetic import captures and explicit demo observations/books",
        "network": "child socket connection, DNS and datagram sends denied; no provider adapter used",
        "deadline_seconds": args.deadline_seconds,
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="crash-recovery-") as temporary:
        report = Path(temporary) / "junit.xml"
        command = [sys.executable, "-m", "pytest", "-q", "tests/test_process_crash_recovery.py",
                   f"--junitxml={report}"]
        evidence["command"] = command
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, start_new_session=True,
                                   env={**os.environ, "PYTHONPATH": str(root / "src")})
        try:
            try:
                stdout, _ = process.communicate(timeout=args.deadline_seconds)
                evidence.update(exit_status=process.returncode, deadline_exceeded=False)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, _ = process.communicate(timeout=5)
                evidence.update(exit_status=124, deadline_exceeded=True)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        evidence["test_output"] = stdout[-16000:]
        cases = []
        if report.is_file():
            for case in ET.parse(report).iter("testcase"):
                status = next((tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None), "passed")
                cases.append({"name": case.attrib["name"], "status": status,
                              "elapsed_seconds": float(case.attrib.get("time", 0)),
                              **SCENARIOS.get(case.attrib["name"], {})})
        evidence["scenarios"] = cases
        evidence["passed"] = (evidence["exit_status"] == 0 and
                              {case["name"] for case in cases} == set(SCENARIOS) and
                              all(case["status"] == "passed" for case in cases))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"passed": evidence["passed"], "scenarios": len(cases), "output": str(output),
                      "source_revision": evidence["source_revision"], "exit_status": evidence["exit_status"]}))
    return 0 if evidence["passed"] else evidence["exit_status"] or 1


if __name__ == "__main__":
    raise SystemExit(main())
