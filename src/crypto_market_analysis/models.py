"""Stable interchange models shared by collection, storage and research."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candle:
    source: str
    asset: str
    quote: str
    timeframe: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    observed_at: int
    quote_volume: float | None = None
    taker_buy_quote_volume: float | None = None
    data_mode: str = "live"


@dataclass(frozen=True)
class Observation:
    source: str
    asset: str
    metric: str
    event_time: int
    observed_at: int
    value: float
    unit: str
    quote: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    data_mode: str = "live"


@dataclass
class CollectionResult:
    source: str
    candles: list[Candle] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    books: list[dict[str, Any]] = field(default_factory=list)
