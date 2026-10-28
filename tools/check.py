"""Run the same offline checks on a local PC or in a cloud workspace."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    commands = (
        ("uv", "lock", "--check", "--offline"),
        (sys.executable, "-m", "ruff", "check", "src", "tests", "tools"),
        (sys.executable, "-m", "pytest", "-q"),
    )
    for command in commands:
        print("Running:", " ".join(command), flush=True)
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
