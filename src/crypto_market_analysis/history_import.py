"""Bounded, retrospective imports of official hourly market-history files.

No extraction, downloads, synthetic bars or historical availability inference occurs.
The returned provenance belongs with the import run, not with an invented event time.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Candle


@dataclass(frozen=True)
class ImportLimits:
    max_file_bytes: int = 256 * 1024 * 1024
    max_member_bytes: int = 64 * 1024 * 1024
    max_members: int = 2000
    max_rows: int = 200_000

    def __post_init__(self):
        for value in vars(self).values():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("Import limits must be positive integers.")


@dataclass(frozen=True)
class HistoryImportResult:
    candles: list[Candle]
    provenance: dict[str, Any]


def _digest(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > limit:
                raise ValueError("History file exceeds the file-size limit.")
            digest.update(block)
    return digest.hexdigest()


def _checksum(expected: str, filename: str) -> str:
    fields = expected.strip().split()
    if not fields or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
        raise ValueError("Expected SHA256 must be a digest or an official checksum line.")
    if len(fields) > 2 or (len(fields) == 2 and fields[1].lstrip("*") != filename):
        raise ValueError("Checksum filename does not match the input archive.")
    return fields[0].lower()


def import_history(
    path: str | Path,
    *,
    source: str,
    asset: str,
    quote: str,
    observed_at: int,
    member: str | None = None,
    expected_sha256: str | None = None,
    timeframe: str = "1h",
    limits: ImportLimits | None = None,
) -> HistoryImportResult:
    """Read Kraken OHLCVT CSV or Binance Vision spot kline CSV/ZIP.

    ZIP member selection is explicit. Binance files require their companion
    checksum (or its digest). Source identity must match the official filename.
    Daily files are deliberately unsupported by the hourly Candle contract.
    """
    limits = limits or ImportLimits()
    path = Path(path)
    asset, quote = asset.upper(), quote.upper()
    if source not in {"kraken", "binance"} or asset not in {"BTC", "ETH"}:
        raise ValueError("Only Kraken/Binance BTC or ETH history is supported.")
    if quote not in {"EUR", "USD", "USDT", "USDC"}:
        raise ValueError("Unsupported native quote; no currency conversion is performed.")
    if timeframe != "1h":
        raise ValueError("Only hourly imports are supported; daily input is not an hourly Candle.")
    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at < 0:
        raise ValueError("Import observation time must be a nonnegative UTC epoch second.")
    if path.stat().st_size > limits.max_file_bytes:
        raise ValueError("History file exceeds the file-size limit.")
    file_hash = _digest(path, limits.max_file_bytes)
    if source == "binance" and expected_sha256 is None:
        raise ValueError("Binance Vision imports require a SHA256 checksum.")
    if expected_sha256 is not None and file_hash != _checksum(expected_sha256, path.name):
        raise ValueError("History file SHA256 checksum mismatch.")
    if zipfile.is_zipfile(path):
        if member is None:
            raise ValueError("Select the exact hourly CSV archive member.")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_members:
                raise ValueError("Archive exceeds the member-count limit.")
            matches = [entry for entry in entries if entry.filename == member]
            if len(matches) != 1:
                raise ValueError("Archive member is missing or duplicated.")
            entry = matches[0]
            safe = PurePosixPath(member)
            if (
                safe.is_absolute()
                or ".." in safe.parts
                or "\\" in member
                or entry.is_dir()
                or entry.flag_bits & 1
                or entry.file_size > limits.max_member_bytes
            ):
                raise ValueError("Archive member is unsafe, encrypted or exceeds the size limit.")
            with archive.open(entry) as stream:
                payload = stream.read(limits.max_member_bytes + 1)
    else:
        if member is not None:
            raise ValueError("Member selection is only valid for ZIP archives.")
        if path.stat().st_size > limits.max_member_bytes:
            raise ValueError("CSV exceeds the member-size limit.")
        payload = path.read_bytes()
    if len(payload) > limits.max_member_bytes:
        raise ValueError("CSV exceeds the member-size limit.")
    name = PurePosixPath(member or path.name).name
    if source == "kraken":
        match = re.fullmatch(r"([A-Z]+)_60\.csv", name)
        symbols = {asset + quote, ("XBT" if asset == "BTC" else asset) + quote}
        if not match or match[1] not in symbols:
            raise ValueError("Kraken filename must identify the selected native market and _60.csv.")
        schema = "kraken_ohlcvt_60_v1"
    else:
        if not re.fullmatch(rf"{asset}{quote}-1h-\d{{4}}-\d{{2}}(?:-\d{{2}})?\.csv", name):
            raise ValueError("Binance filename must identify the selected native spot market and 1h.")
        schema = "binance_vision_spot_1h_v1"
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
        rows: dict[int, Candle] = {}
        row_count = 0
        for row in reader:
            row_count += 1
            if row_count > limits.max_rows:
                raise ValueError("CSV exceeds the row-count limit.")
            if not row:
                raise ValueError(f"Empty CSV row {row_count}.")
            try:
                if len(row) != (7 if source == "kraken" else 12):
                    raise ValueError("Unexpected column count.")
                timestamp = int(row[0])
                if source == "binance":
                    # Official spot archive migration is date-based, not caller-selected.
                    divisor = 1_000_000 if timestamp >= 1_000_000_000_000_000 else 1000
                    if timestamp % divisor:
                        raise ValueError("Non-integral open timestamp.")
                    timestamp //= divisor
                    if (timestamp >= 1735689600) != (divisor == 1_000_000):
                        raise ValueError("Spot archive timestamp unit contradicts the 2025 migration.")
                    close_stamp = int(row[6])
                    if close_stamp != (timestamp + 3600) * divisor - 1:
                        raise ValueError("Input is not a complete hourly kline.")
                if timestamp < 0 or timestamp % 3600 or timestamp + 3600 > observed_at:
                    raise ValueError("Candle must be aligned, complete and observed at import time.")
                op, high, low, close, volume = map(float, row[1:6])
                if (
                    not all(math.isfinite(v) for v in (op, high, low, close, volume))
                    or min(op, high, low, close) <= 0
                    or volume < 0
                    or high < max(op, low, close)
                    or low > min(op, high, close)
                ):
                    raise ValueError("Invalid OHLCV values.")
                quote_volume = taker = None
                if source == "binance":
                    quote_volume, taker = float(row[7]), float(row[10])
                    if (
                        not all(math.isfinite(v) and v >= 0 for v in (quote_volume, taker))
                        or taker > quote_volume
                    ):
                        raise ValueError("Invalid quoted or taker-buy volume.")
                    int(row[8])  # Trade count belongs to the source schema.
                elif int(row[6]) < 0:
                    raise ValueError("Negative trade count.")
                candle = Candle(
                    source,
                    asset,
                    quote,
                    "1h",
                    timestamp,
                    timestamp + 3600,
                    op,
                    high,
                    low,
                    close,
                    volume,
                    observed_at,
                    quote_volume,
                    taker,
                    "import",
                )
                if timestamp in rows and rows[timestamp] != candle:
                    raise ValueError("Conflicting duplicate candle.")
                rows[timestamp] = candle
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"Invalid history CSV row {row_count}: {exc}") from exc
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("History CSV is not valid UTF-8 CSV.") from exc
    candles = [rows[t] for t in sorted(rows)]
    if not candles:
        raise ValueError("History file contains no candles.")
    return HistoryImportResult(
        candles,
        {
            "source": source,
            "asset": asset,
            "quote": quote,
            "timeframe": "1h",
            "data_mode": "import",
            "filename": path.name,
            "member": member,
            "file_sha256": file_hash,
            "member_sha256": hashlib.sha256(payload).hexdigest(),
            "data_version": "sha256:" + file_hash,
            "schema": schema,
            "checksum_verified": expected_sha256 is not None,
            "observed_at": observed_at,
            "historical_known_at": "unknown",
            "availability": "retrospective",
            "rows_read": row_count,
            "candles": len(candles),
            "first_open_time": candles[0].open_time,
            "last_close_time": candles[-1].close_time,
            "missing_hour_count": (candles[-1].open_time - candles[0].open_time) // 3600 + 1 - len(candles),
        },
    )
