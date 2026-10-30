"""Synthetic local recovery demonstration; run with PYTHONPATH=src python this_file.

All files live in a temporary directory. This collects no data, creates no future
observations and never opens an existing personal database.
"""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle
from crypto_market_analysis.recovery import create_backup, replay_assessment, restore_backup, verify_backup


def main():
    with TemporaryDirectory(prefix="synthetic-recovery-") as temporary:
        directory = Path(temporary)
        engine = Engine(Settings(data_dir=directory / "source"), "demo")
        end = int(time.time()) // 3600 * 3600
        engine.store.put_candles([
            Candle("synthetic", "BTC", "EUR", "1h", end - 3600, end,
                   100, 100, 100, 100, 1, int(time.time()), data_mode="demo"),
        ])
        original = engine.issue_assessment("synthetic-recovery-example", "BTC", "synthetic", "EUR")
        backup = create_backup(engine.store.path, directory / "backup")
        checked = verify_backup(directory / "backup")
        restored = restore_backup(directory / "backup", directory / "restored")
        replayed = replay_assessment(original)
        print(json.dumps({"data_mode": "demo", "backup": backup["status"],
                          "verification": checked["verification"], "restore": restored["status"],
                          "replay": replayed["status"], "scope": replayed["scope"]}, indent=2))


if __name__ == "__main__":
    main()
