"""Small TOML configuration. Secrets and paid collection endpoints are unnecessary."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .venues import VENUES


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
    return settings
