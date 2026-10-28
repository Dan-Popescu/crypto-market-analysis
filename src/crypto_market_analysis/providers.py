"""Bounded, unauthenticated public options, network and macro feeds."""

from __future__ import annotations

import csv
import io
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .models import CollectionResult, Observation


class PublicHTTP:
    def __init__(self, timeout: float = 10):
        self.client = httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": "crypto-market-analysis/0.1"}
        )

    def close(self):
        self.client.close()

    def _get(self, url: str, params: dict | None = None):
        for attempt in range(2):
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if attempt or not (status == 429 or 500 <= status < 600):
                    raise
                try:
                    delay = float(exc.response.headers.get("retry-after", "0.25"))
                except ValueError:
                    delay = 0.25
                time.sleep(min(max(delay, 0), 1))
            except httpx.TransportError:
                if attempt:
                    raise
                time.sleep(0.25)
        raise RuntimeError("unreachable")

    def get_json(self, url: str, params: dict | None = None):
        return self._get(url, params).json()

    def get_text(self, url: str, params: dict | None = None):
        return self._get(url, params).text


def _date(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=UTC).timestamp())


def parse_options(instruments: list[dict], summaries: list[dict], asset: str, now: int) -> list[Observation]:
    """Select liquid near-ATM IV nearest target expiry and aggregate expiry OI."""
    lookup = {i.get("instrument_name"): i for i in instruments}
    rows = []
    expiry_oi: dict[int, float] = {}
    for s in summaries:
        i = lookup.get(s.get("instrument_name"))
        if not i:
            continue
        try:
            expiry = int(i["expiration_timestamp"]) // 1000
            oi = float(s.get("open_interest") or 0)
            if expiry <= now or not math.isfinite(oi) or oi < 0:
                continue
            expiry_oi[expiry] = expiry_oi.get(expiry, 0) + oi
            bid, ask = float(s.get("bid_price") or 0), float(s.get("ask_price") or 0)
            iv = float(s.get("mark_iv") or 0)
            underlying = float(s.get("underlying_price") or 0)
            strike = float(i["strike"])
            if (
                not all(math.isfinite(v) for v in (bid, ask, underlying, iv, strike))
                or bid <= 0
                or ask < bid
                or underlying <= 0
                or iv <= 0
                or oi <= 0
            ):
                continue
            spread = (ask - bid) / ((ask + bid) / 2)
            if spread > 0.5 or abs(strike / underlying - 1) > 0.15:
                continue
            rows.append((expiry, abs(strike / underlying - 1), spread, iv, i, oi, underlying))
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            continue
    out = []
    for expiry, oi in sorted(expiry_oi.items())[:64]:
        out.append(
            Observation(
                "deribit",
                asset,
                "option_open_interest_expiry",
                now,
                now,
                oi,
                asset,
                asset,
                {"expiry": expiry, "aggregation": "calls_and_puts", "source_timestamp": "snapshot"},
            )
        )
    for days in (7, 30, 60):
        if not rows:
            break
        row = min(rows, key=lambda r: (abs(r[0] - (now + days * 86400)), r[1], r[2]))
        expiry, distance, spread, iv, i, oi, underlying = row
        if abs(expiry - (now + days * 86400)) > {7: 4, 30: 10, 60: 15}[days] * 86400:
            continue
        out.append(
            Observation(
                "deribit",
                asset,
                f"option_atm_iv_{days}d",
                now,
                now,
                iv,
                "percent",
                asset,
                {
                    "target_days": days,
                    "expiry": expiry,
                    "instrument": i["instrument_name"],
                    "option_type": i.get("option_type"),
                    "strike": i.get("strike"),
                    "moneyness_distance": distance,
                    "relative_spread": spread,
                    "open_interest": oi,
                    "interpretation": "quoted IV, not a calibrated probability",
                },
            )
        )
    # Compare out-of-the-money puts and calls at the same expiry and similar distance.
    for expiry in sorted({r[0] for r in rows})[:64]:
        puts = [
            r
            for r in rows
            if r[0] == expiry and r[4].get("option_type") == "put" and float(r[4]["strike"]) < r[6]
        ]
        calls = [
            r
            for r in rows
            if r[0] == expiry and r[4].get("option_type") == "call" and float(r[4]["strike"]) > r[6]
        ]
        if not puts or not calls:
            continue
        pairs = [(p, c) for p in puts for c in calls if abs(p[1] - c[1]) <= 0.05]
        if not pairs:
            continue
        put, call = min(pairs, key=lambda pair: (abs(pair[0][1] - pair[1][1]), pair[0][1] + pair[1][1]))
        out.append(
            Observation(
                "deribit",
                asset,
                "option_downside_minus_upside_iv",
                now,
                now,
                put[3] - call[3],
                "percentage_points",
                asset,
                {
                    "expiry": expiry,
                    "put_instrument": put[4]["instrument_name"],
                    "call_instrument": call[4]["instrument_name"],
                    "put_strike": put[4]["strike"],
                    "call_strike": call[4]["strike"],
                    "put_moneyness_distance": put[1],
                    "call_moneyness_distance": call[1],
                    "interpretation": "matched expiry and approximate moneyness, not delta",
                },
            )
        )
    return out


def collect_options(assets: list[str], now: int | None = None, *, client: Any = None) -> CollectionResult:
    now = int(time.time()) if now is None else int(now)
    own_client = client is None
    result = CollectionResult("deribit")
    if own_client:
        try:
            client = PublicHTTP()
        except Exception as exc:
            result.errors.append(f"Deribit HTTP client: {type(exc).__name__}: {exc}")
            return result
    for asset in dict.fromkeys(a.upper() for a in assets):
        try:
            root = "https://www.deribit.com/api/v2/public/"
            instruments = client.get_json(
                root + "get_instruments", {"currency": asset, "kind": "option", "expired": "false"}
            )
            summaries = client.get_json(
                root + "get_book_summary_by_currency", {"currency": asset, "kind": "option"}
            )
            if "error" in instruments or "error" in summaries:
                raise ValueError(str(instruments.get("error") or summaries.get("error")))
            observations = parse_options(instruments["result"], summaries["result"], asset, now)
            result.observations.extend(observations)
            if not observations:
                result.warnings.append(f"{asset}: no usable option summaries")
        except Exception as exc:
            result.errors.append(f"{asset} Deribit public options: {type(exc).__name__}: {exc}")
    if own_client:
        client.close()
    return result


def collect_context(now: int | None = None, days: int = 365, *, client: Any = None) -> list[CollectionResult]:
    now = int(time.time()) if now is None else int(now)
    days = max(1, min(int(days), 3650))
    start = now - days * 86400
    own_client = client is None
    if own_client:
        try:
            client = PublicHTTP()
        except Exception as exc:
            return [
                CollectionResult(source, errors=[f"HTTP client: {type(exc).__name__}: {exc}"])
                for source in ("coinmetrics", "defillama", "fred")
            ]
    results = []
    network = CollectionResult("coinmetrics")
    for asset in ("BTC", "ETH"):
        for metric in ("AdrActCnt", "TxCnt"):
            try:
                data = client.get_json(
                    "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                    {
                        "assets": asset.lower(),
                        "metrics": metric,
                        "frequency": "1d",
                        "start_time": datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d"),
                        "page_size": min(days + 2, 10000),
                    },
                )
                for row in data.get("data", []):
                    ts = _date(row["time"])
                    if (
                        start <= ts <= now
                        and row.get(metric) is not None
                        and math.isfinite(float(row[metric]))
                    ):
                        network.observations.append(
                            Observation(
                                "coinmetrics",
                                asset,
                                metric,
                                ts,
                                now,
                                float(row[metric]),
                                "USD" if metric.endswith("USD") else "count",
                            )
                        )
                if data.get("next_page_url"):
                    network.warnings.append(f"{asset} {metric}: response truncated; next page not fetched")
            except Exception as exc:
                network.errors.append(f"{asset} {metric} Coin Metrics: {type(exc).__name__}: {exc}")
    results.append(network)
    stable = CollectionResult("defillama")
    try:
        rows = client.get_json("https://stablecoins.llama.fi/stablecoincharts/all")
        for row in rows:
            ts = int(row["date"])
            value = (row.get("totalCirculatingUSD") or {}).get("peggedUSD")
            if start <= ts <= now and value is not None and math.isfinite(float(value)):
                stable.observations.append(
                    Observation(
                        "defillama",
                        "STABLECOINS",
                        "stablecoin_supply",
                        ts,
                        now,
                        float(value),
                        "USD",
                        "USD",
                        {"scope": "all chains", "peg": "USD"},
                    )
                )
    except Exception as exc:
        stable.errors.append(f"DefiLlama stablecoincharts/all: {type(exc).__name__}: {exc}")
    results.append(stable)
    fred = CollectionResult("fred")
    for series in ("DFII10", "DTWEXBGS", "DGS2", "DGS10", "VIXCLS"):
        try:
            raw = client.get_text(
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                {"id": series, "cosd": datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d")},
            )
            for row in csv.DictReader(io.StringIO(raw)):
                date = row.get("observation_date") or row.get("DATE")
                value = row.get(series)
                if not date or value in (None, "", "."):
                    continue
                ts = _date(date)
                if start <= ts <= now and math.isfinite(float(value)):
                    fred.observations.append(
                        Observation(
                            "fred",
                            "MACRO",
                            series,
                            ts,
                            now,
                            float(value),
                            "index" if series in ("DTWEXBGS", "VIXCLS") else "percent",
                            metadata={"vintage": "current", "historical_known_at": "unknown"},
                        )
                    )
        except Exception as exc:
            fred.errors.append(f"{series} FRED CSV: {type(exc).__name__}: {exc}")
    results.append(fred)
    if own_client:
        client.close()
    return results
