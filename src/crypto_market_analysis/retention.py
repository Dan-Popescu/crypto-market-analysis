"""Compact old liquidity snapshots; historical intraday replay is lost after compaction."""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict

from .models import Observation
from .storage import Store

# Retain all other observation families and already-derived aggregates.
_RAW = """(metric='spread_bps' OR metric GLOB 'depth_*')
          AND json_extract(payload, '$.metadata.aggregation') IS NULL"""


def compact_liquidity(store: Store, keep_days: int = 7, now: int | None = None) -> dict:
    """Compact one oldest UTC day per call, retaining complete UTC hours only.

    Derived records become available at compaction time. Raw intraday history, including
    older versions, is removed after the aggregate is safely written.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ValueError("keep_days must be a positive integer")
    now = int(time.time()) if now is None else int(now)
    cutoff = ((now - keep_days * 86400) // 3600) * 3600
    with store.connection() as con:
        oldest = con.execute(
            f"SELECT MIN(event_time) FROM observations WHERE {_RAW} AND event_time < ? AND observed_at <= ?",
            (cutoff, now),
        ).fetchone()[0]
        if oldest is None:
            return {"aggregates_written": 0, "raw_rows_deleted": 0, "remaining_old_rows": 0}
        day_start = (oldest // 86400) * 86400
        end = min(day_start + 86400, cutoff)
        rows = con.execute(
            f"""SELECT * FROM (SELECT id,source,asset,metric,event_time,data_mode,dimension,payload,
            ROW_NUMBER() OVER (PARTITION BY source,asset,metric,event_time,data_mode,dimension
            ORDER BY observed_at DESC,id DESC) AS rn FROM observations
            WHERE {_RAW} AND event_time >= ? AND event_time < ? AND observed_at <= ?)
            WHERE rn=1 ORDER BY event_time""",
            (day_start, end, now),
        ).fetchall()
        ids = [
            r[0]
            for r in con.execute(
                f"SELECT id FROM observations WHERE {_RAW} AND event_time >= ? AND event_time < ? AND observed_at <= ?",
                (day_start, end, now),
            )
        ]

    groups = defaultdict(list)
    for row in rows:
        payload = json.loads(row["payload"])
        key = (
            row["source"],
            row["asset"],
            row["metric"],
            row["data_mode"],
            row["dimension"],
            (row["event_time"] // 3600 + 1) * 3600,
        )
        groups[key].append(payload)
    aggregates = []
    for (source, asset, metric, mode, dimension, hour_end), samples in groups.items():
        values = [sample["value"] for sample in samples]
        flags = [sample.get("metadata", {}).get("coverage_complete") is True for sample in samples]
        first = samples[0]
        original = json.loads(dimension)
        metadata = {
            "aggregation": "hourly_median",
            "min": min(values),
            "max": max(values),
            "sample_count": len(values),
            "sample_window_first": samples[0]["event_time"],
            "sample_window_last": samples[-1]["event_time"],
            "coverage_complete": all(flags),
            "coverage_fraction": sum(flags) / len(flags),
            # Separate aggregates from a raw sample at the hour boundary in Store's logical key.
            "symbol": "retention-hourly:" + dimension,
            "original_dimension": original,
        }
        band = first.get("metadata", {}).get("band_percent")
        if band is not None:
            metadata["band_percent"] = band
        aggregates.append(
            Observation(
                source,
                asset,
                metric,
                hour_end,
                now,
                statistics.median(values),
                first["unit"],
                first.get("quote"),
                metadata,
                mode,
            )
        )

    inserted = store.put_observations(aggregates)
    # Delete only IDs captured before the successful write; late concurrent inserts remain intact.
    deleted = 0
    with store.connection() as con:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            deleted += con.execute(f"DELETE FROM observations WHERE id IN ({placeholders})", batch).rowcount
        remaining = con.execute(
            f"SELECT COUNT(*) FROM observations WHERE {_RAW} AND event_time < ? AND observed_at <= ?",
            (cutoff, now),
        ).fetchone()[0]
    return {"aggregates_written": inserted, "raw_rows_deleted": deleted, "remaining_old_rows": remaining}
