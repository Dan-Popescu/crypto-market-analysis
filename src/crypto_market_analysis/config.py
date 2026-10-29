"""Small TOML configuration. Secrets and paid collection endpoints are unnecessary."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .venues import FRANCE_PURCHASE_CANDIDATES, VENUES


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    assets: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    venues: list[str] = field(default_factory=lambda: list(VENUES))
    history_days: int = 30
    max_pages: int = 20
    workers: int = 3
    options: bool = True
    context: bool = True
    liquidity_interval: int = 300
    market_interval: int = 3600
    options_interval: int = 21600
    context_interval: int = 86400
    liquidity_retention_days: int = 7
    storage_maintenance_enabled: bool = False
    storage_hot_max_bytes: int = 2 * 1024**3
    storage_archive_max_bytes: int = 4 * 1024**3
    storage_archive_max_files: int = 4096
    storage_book_hot_max_bytes: int = 512 * 1024**2
    storage_book_keep_days: int = 7
    storage_run_keep_days: int = 30
    storage_batch_rows: int = 256
    storage_write_reserve_bytes: int = 8 * 1024**2

    # Explicit native markets; stablecoin quotes are never converted to USD.
    quote_currencies: list[str] = field(default_factory=lambda: ["EUR", "USD", "USDT", "USDC"])
    purchase_jurisdiction: str = "France"
    purchase_candidates: list[str] = field(default_factory=lambda: list(FRANCE_PURCHASE_CANDIDATES))
    # None samples all analytical venues; a subset bounds book requests only.
    liquidity_venues: list[str] | None = None

    @property
    def database(self) -> Path:
        return self.data_dir / "observer.sqlite"


def load_settings(path: Path | None = None, data_dir: Path | None = None) -> Settings:
    raw = {}
    if path is not None:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    known = set(Settings.__dataclass_fields__)
    if set(raw) - known:
        raise ValueError(f"Unknown configuration options: {sorted(set(raw) - known)}")
    if "data_dir" in raw:
        directory = Path(raw["data_dir"]).expanduser()
        raw["data_dir"] = directory if directory.is_absolute() else path.resolve().parent / directory
    settings = Settings(**raw)
    if data_dir is not None:
        settings.data_dir = data_dir.expanduser().resolve()
    if not settings.assets or set(settings.assets) - {"BTC", "ETH"}:
        raise ValueError("assets must contain BTC and/or ETH.")
    if not settings.venues or set(settings.venues) - set(VENUES):
        raise ValueError("Unknown venue; use the venues command.")
    if (
        not isinstance(settings.quote_currencies, list)
        or not settings.quote_currencies
        or any(q not in ("EUR", "USD", "USDT", "USDC") for q in settings.quote_currencies)
        or len(set(settings.quote_currencies)) != len(settings.quote_currencies)
    ):
        raise ValueError("quote_currencies must contain distinct native EUR, USD, USDT or USDC quotes.")
    if settings.purchase_jurisdiction != "France":
        raise ValueError("purchase_jurisdiction currently supports France only.")
    if not isinstance(settings.purchase_candidates, list) or any(
        v not in VENUES for v in settings.purchase_candidates
    ):
        raise ValueError("purchase_candidates must contain known venues; eligibility remains unverified.")
    if settings.liquidity_venues is not None and (
        not isinstance(settings.liquidity_venues, list)
        or any(v not in settings.venues for v in settings.liquidity_venues)
    ):
        raise ValueError("liquidity_venues must be a subset of analytical venues.")
    for name, low, high in (
        ("workers", 1, 8),
        ("history_days", 1, 3650),
        ("max_pages", 1, 100),
        ("liquidity_interval", 60, 86400),
        ("market_interval", 300, 86400),
        ("options_interval", 3600, 604800),
        ("context_interval", 3600, 604800),
        ("liquidity_retention_days", 1, 365),
    ):
        value = getattr(settings, name)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}.")
    if type(settings.storage_maintenance_enabled) is not bool:
        raise ValueError("storage_maintenance_enabled must be a boolean.")
    from .storage_policy import StoragePolicy

    StoragePolicy.from_settings(settings)
    return settings
