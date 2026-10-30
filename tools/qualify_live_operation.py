"""Explicit bounded live qualification, or read-only status without --refresh.

Example: uv run python tools/qualify_live_operation.py --data-dir data \
  --refresh --session-id prospective --max-cycles 12 --max-duration-seconds 3600
Resume with the same session and selection; no daemon or background job starts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from crypto_market_analysis.config import load_settings
from crypto_market_analysis.operating_acceptance import run_operating_acceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Explicitly permit finite public live collection.")
    parser.add_argument("--session-id", default="default")
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--max-duration-seconds", type=int, default=60)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--cycle-http-requests", type=int, default=1000)
    parser.add_argument("--cycle-deadline-seconds", type=float, default=30)
    parser.add_argument("--required-history-hours", type=int, default=24)
    parser.add_argument("--tasks", nargs="+", default=["market"], choices=("market", "liquidity", "options", "context"))
    parser.add_argument("--output", type=Path, help="Optional JSON report path; otherwise print JSON.")
    args = parser.parse_args()
    settings = load_settings(args.config, args.data_dir)
    request = {key: getattr(args, key) for key in ("refresh", "session_id", "max_cycles",
        "max_duration_seconds", "interval_seconds", "cycle_http_requests", "cycle_deadline_seconds",
        "required_history_hours", "tasks")}
    try:
        report = run_operating_acceptance(settings, request)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Operating qualification failed: {exc}\n")
    body = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
