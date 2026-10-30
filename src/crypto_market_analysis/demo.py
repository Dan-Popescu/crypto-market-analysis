"""Explicit synthetic fixtures for an offline end-to-end run, never a fallback."""

from __future__ import annotations

import math
import time

import numpy as np

from .models import Candle, Observation
from .storage import Store


def seed_demo(store: Store, days: int = 1800, now: int | None = None, quotes: tuple[str, ...] = ("USD",)) -> dict:
    if not 90 <= days <= 3650:
        raise ValueError("The demonstration requires between 90 and 3650 days.")
    if not quotes or len(set(quotes)) != len(quotes) or any(quote not in ("USD", "EUR") for quote in quotes):
        raise ValueError("Synthetic quotes must be distinct EUR and/or USD values.")
    end = (int(time.time()) if now is None else now) // 3600 * 3600
    start = end // 86400 * 86400 - days * 86400
    count = 0
    markets = [(asset, quote, usd if quote == "USD" else eur, 840 + offset + (100 if quote == "EUR" else 0))
               for offset, (asset, usd, eur) in enumerate((("BTC", 30000.0, 25000.0), ("ETH", 1800.0, 1500.0)))
               for quote in quotes]
    for asset, quote, initial, seed in markets:
        rng = np.random.default_rng(seed)
        n = (end - start) // 3600
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
                    quote,
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
                ("depth_bid_1pct", 5e6 * (1 + 0.3 * wave), quote, {"coverage_complete": True}),
                ("depth_ask_1pct", 5e6 * (1 - 0.2 * wave), quote, {"coverage_complete": True}),
                ("open_interest_value", 1e9 * (1 + 0.1 * wave), quote, {}),
                ("funding_rate_actual", 0.0001 + 0.0002 * wave, "rate", {"funding_interval_hours": 8}),
                ("option_atm_iv_30d", 55 + 10 * wave, "percent", {"expiry": end + 30 * 86400}),
            ):
                observations.append(
                    Observation("demo", asset, metric, timestamp, end, value, unit, quote, metadata, "demo")
                )
        store.put_observations(observations)
    return {
        "data_mode": "demo",
        "notice": "Synthetic data: no real market quotes.",
        "days": days,
        "end": end,
        "quotes": list(quotes),
        "quote_relationship": "Independent synthetic scenarios; no measured FX conversion.",
        "inserted_candles": count,
    }
