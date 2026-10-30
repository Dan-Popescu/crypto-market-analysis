"""Reproducible offline mixed-class workloads; no live capacity or cost claim."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

START = 1704153600  # 2024-01-02 UTC; all prices and identities below are invented.


def synthetic_bundle(instruments=5, *, vintages=2):
    """Build five native instrument forms with daily sessions and point references."""
    if type(instruments) is not int or not 1 <= instruments <= 1000 or vintages not in {1, 2}:
        raise ValueError("Synthetic workload requires 1..1000 instruments and 1 or 2 vintages.")
    end = START + 10 * 86400
    dates = [(day, datetime.fromtimestamp(START + day * 86400, UTC).date()) for day in range(10)]
    weekdays = [(day, date) for day, date in dates if date.weekday() < 5]
    base = {"schema": "market-calendar.v1", "version": "synthetic-v1", "timezone": "UTC",
            "source_url": "https://example.invalid/synthetic-schedule", "known_at": START - 1,
            "supported_start": START, "supported_end": end}
    exchange = {**base, "id": "synthetic:exchange", "profile": "explicit", "sessions": [
        {"date": str(date), "segments": [{"start": START + day * 86400 + 9 * 3600,
                                          "end": START + day * 86400 + 16 * 3600}]}
        for day, date in weekdays]}
    continuous = {**base, "id": "synthetic:continuous", "profile": "continuous", "sessions": []}
    reference = {**base, "id": "synthetic:publication", "profile": "explicit", "sessions": [
        {"date": str(date), "segments": [], "publication_at": START + day * 86400 + 18 * 3600}
        for day, date in weekdays]}
    registry = {"schema": "market-registry.v1", "instruments": [], "listings": [], "aliases": [], "memberships": []}
    series = []
    for index in range(instruments):
        kind = index % 5
        asset_class, form = [("equity", "share"), ("crypto", "spot"), ("commodity", "reference"),
                             ("commodity", "fund"), ("commodity", "future")][kind]
        iid, sid, symbol = f"synthetic:instrument:{index}", f"synthetic:series:{index}", f"EXAMPLE{index}"
        native = "EUR" if index % 2 else "USD"
        instrument = {"id": iid, "name": f"Synthetic {form} {index}", "asset_class": asset_class, "form": form}
        if kind == 4:
            instrument["contract"] = {"expiry": end + 30 * 86400, "unit": "barrel", "multiplier": 1000,
                                      "settlement": "cash"}
        registry["instruments"].append(instrument)
        calendar = reference if kind == 2 else continuous if kind == 1 else exchange
        listing_id = None if kind == 2 else f"synthetic:listing:{index}"
        if listing_id is not None:
            registry["listings"].append({"id": listing_id, "instrument_id": iid, "venue_id": "synthetic",
                                          "native_currency": native, "calendar_id": calendar["id"],
                                          "active_from": START, "active_until": None, "known_at": START - 1})
        registry["aliases"].append({"provider": "synthetic", "symbol": symbol, "instrument_id": iid,
                                    "listing_id": listing_id, "effective_from": START, "effective_until": None,
                                    "known_at": START - 1})
        registry["memberships"].append({"universe": "synthetic", "instrument_id": iid, "valid_from": START,
                                        "valid_until": None, "known_at": START - 1})
        descriptor = {"id": sid, "instrument_id": iid, "listing_id": listing_id, "provider": "synthetic",
                      "provider_symbol": symbol, "native_currency": native,
                      "unit": "barrel" if kind in {2, 4} else "share" if kind in {0, 3} else "coin",
                      "price_basis": "raw", "data_mode": "demo", "calendar_id": calendar["id"],
                      "calendar_version": calendar["version"], "interval": "reference" if kind == 2 else "session",
                      "revision_semantics": "point_in_time", "source_url": "https://example.invalid/synthetic-prices",
                      "rights": "Invented demonstration data; not observed market prices."}
        bars = []
        for day, date in dates if kind == 1 else weekdays:
            opened = START + day * 86400 + (18 * 3600 if kind == 2 else 0 if kind == 1 else 9 * 3600)
            closed = opened if kind == 2 else START + (day + 1) * 86400 if kind == 1 else START + day * 86400 + 16 * 3600
            price = -20 + day if kind in {2, 4} else 100 + index + day
            for vintage in range(vintages):
                value = price + vintage / 10
                bars.append({"start": opened, "end": closed, "open": value, "high": value, "low": value,
                             "close": value, "volume": 1, "observed_at": closed + vintage,
                             "session_date": str(date)})
        series.append({"schema": "market-series.v1", "descriptor": descriptor, "bars": bars, "actions": []})
    return {"schema": "market-bundle.v1", "registry": registry, "calendars": [exchange, continuous, reference], "series": series}


def run_workload(directory, count):
    from crypto_market_analysis.cross_asset.application import validate_bundle
    from crypto_market_analysis.cross_asset.snapshots import MarketSnapshot, export_market_snapshot
    from crypto_market_analysis.cross_asset.storage import CrossAssetStore

    bundle = synthetic_bundle(count)
    began = time.perf_counter()
    bundle = validate_bundle(bundle)
    validation = time.perf_counter() - began
    store = CrossAssetStore(directory / "observer.sqlite", initialize=True)
    began = time.perf_counter()
    store.import_bundle(bundle, received_at=int(time.time()))
    ingest = time.perf_counter() - began
    as_of = START + 10 * 86400
    began = time.perf_counter()
    original = store.read_series("synthetic:series:0", as_of=as_of, data_mode="demo")
    sqlite_read = time.perf_counter() - began
    began = time.perf_counter()
    manifest = export_market_snapshot(store, directory / "snapshot", max_rows=100_000)
    export = time.perf_counter() - began
    began = time.perf_counter()
    snapshot = MarketSnapshot(directory / "snapshot")
    frozen = snapshot.read_series("synthetic:series:0", as_of=as_of, data_mode="demo")
    first_read = time.perf_counter() - began
    if frozen != original:
        raise AssertionError("SQLite and immutable snapshot native evidence differ.")
    result = {"instruments": count, "listings": len(bundle["registry"]["listings"]), "series": count,
              "bar_vintages": sum(len(row["bars"]) for row in bundle["series"]),
              "seconds": {"validate": validation, "ingest": ingest, "sqlite_single_series": sqlite_read,
                          "snapshot_export": export, "first_verified_snapshot_read": first_read},
              "native_evidence_equal": True, "snapshot_format": manifest.get("format"),
              "source_http_requests": 0, "provider_capacity_qualified": False}
    if hasattr(snapshot, "fleet_summary"):
        ids = [row["descriptor"]["id"] for row in bundle["series"]]
        began = time.perf_counter()
        summary = snapshot.fleet_summary(ids, as_of=as_of, data_mode="demo", start=START, end=as_of)
        result["seconds"]["fleet_summary"] = time.perf_counter() - began
        result["fleet_summary"] = summary
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="market-scale-") as temporary:
        root = Path(temporary)
        results = []
        for index, count in enumerate(args.instruments):
            directory = root / str(index)
            directory.mkdir()
            results.append(run_workload(directory, count))
    report = {"schema": "market-synthetic-benchmark.v1", "results": results,
              "limitations": ["Synthetic daily/point observations only; no real source, licensing or concurrency acceptance.",
                              "First verification is not an OS cold-cache experiment."]}
    content = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
