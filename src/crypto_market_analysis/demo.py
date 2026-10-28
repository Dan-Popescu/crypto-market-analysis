"""Explicit synthetic fixtures for an offline end-to-end run, never a fallback."""

from __future__ import annotations

import math
import time

import numpy as np

from .models import Candle, Observation
from .storage import Store


def seed_demo(store: Store, days: int = 1800, now: int | None = None) -> dict:
    if not 90 <= days <= 3650:
        raise ValueError("The demonstration requires between 90 and 3650 days.")
    end = (int(time.time()) if now is None else now) // 86400 * 86400
    start = end - days * 86400
    count = 0
    for offset, (asset, initial) in enumerate((("BTC", 30000.0), ("ETH", 1800.0))):
        rng = np.random.default_rng(840 + offset)
        n = days * 24
        t = np.arange(n)
        drift = 0.000025 + np.sin(t / (24 * 90)) * 0.00012
        returns = rng.normal(drift, 0.006 * (1 + 0.4 * np.sin(t / 600)))
        prices = initial * np.exp(np.cumsum(np.clip(returns, -0.08, 0.08)))
        candles = []
        for i, close in enumerate(prices):
            open_price = initial if i == 0 else float(prices[i - 1])
            volume = float(rng.uniform(10, 500))
            quote_volume = volume * (open_price + float(close)) / 2
            candles.append(
                Candle(
                    "demo",
                    asset,
                    "USD",
                    "1h",
                    start + i * 3600,
                    start + (i + 1) * 3600,
                    open_price,
                    max(open_price, float(close)) * 1.004,
                    min(open_price, float(close)) * 0.996,
                    float(close),
                    volume,
                    end,
                    quote_volume,
                    quote_volume * float(rng.uniform(0.35, 0.65)),
                    "demo",
                )
            )
        count += store.put_candles(candles)
        observations = []
        for i in range(28 * 24):
            timestamp = end - (28 * 24 - i) * 3600
            wave = math.sin(i / 40)
            for metric, value, unit, metadata in (
                ("depth_bid_1pct", 5e6 * (1 + 0.3 * wave), "USD", {"coverage_complete": True}),
                ("depth_ask_1pct", 5e6 * (1 - 0.2 * wave), "USD", {"coverage_complete": True}),
                ("open_interest_value", 1e9 * (1 + 0.1 * wave), "USD", {}),
                ("funding_rate_actual", 0.0001 + 0.0002 * wave, "rate", {"funding_interval_hours": 8}),
                ("option_atm_iv_30d", 55 + 10 * wave, "percent", {"expiry": end + 30 * 86400}),
            ):
                observations.append(
                    Observation("demo", asset, metric, timestamp, end, value, unit, "USD", metadata, "demo")
                )
        store.put_observations(observations)
    return {
        "data_mode": "demo",
        "notice": "Synthetic data: no real market quotes.",
        "days": days,
        "end": end,
        "inserted_candles": count,
    }
