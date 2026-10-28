"""Compact liquidity hours; retain late raw evidence without inventing a new median."""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, replace

from .evidence import content_hash
from .models import Observation
from .storage import Store

# Quarantined late samples are derived records, never raw vigilance baselines or
# pending maintenance. Other observation families are retained unchanged.
_RAW = """(metric='spread_bps' OR metric GLOB 'depth_*')
          AND json_extract(payload, '$.metadata.aggregation') IS NULL"""


def _key(row) -> tuple:
    return (row["source"], row["asset"], row["metric"], row["data_mode"], row["dimension"],
            (row["event_time"] // 3600 + 1) * 3600)


def _covered(row, aggregate: Observation) -> bool:
    """Recognize a successfully written snapshot whose raw deletion was interrupted."""
    return (row["id"] <= aggregate.metadata.get("compaction_raw_max_id", -1)
            and row["observed_at"] <= aggregate.metadata.get("compaction_available_at", -1))


def compact_liquidity(store: Store, keep_days: int = 7, now: int | None = None) -> dict:
    """Compact one oldest UTC day per call, retaining complete UTC hours only.

    Finalized hours cannot be rebuilt from medians. Their late raw versions are
    retained as explicitly unmerged derived observations; the original median and
    sample count survive with a new partial-coverage version available at ``now``.
    Writes precede deletion of captured IDs. Snapshot watermarks make a retry after
    a successful write and interrupted deletion safe without reprocessing samples.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ValueError("keep_days must be a positive integer")
    now = int(time.time()) if now is None else int(now)
    # A separate, data-free SQLite lock serializes competing compactors without
    # blocking collectors. SQLite releases it on close/crash; waits are bounded.
    lock_path = store.path.with_name(store.path.stem + ".retention-lock.sqlite")
    lock = sqlite3.connect(lock_path, timeout=15)
    try:
        lock.execute("BEGIN IMMEDIATE")
        return _compact_day(store, keep_days, now)
    finally:
        lock.close()


def _compact_day(store: Store, keep_days: int, now: int) -> dict:
    cutoff = ((now - keep_days * 86400) // 3600) * 3600
    with store.connection() as con:
        # All reads must describe one snapshot, including versions and IDs to delete.
        con.execute("BEGIN")
        oldest = con.execute(
            f"SELECT MIN(event_time) FROM observations WHERE {_RAW} AND event_time < ? AND observed_at <= ?",
            (cutoff, now),
        ).fetchone()[0]
        if oldest is None:
            return {"aggregates_written": 0, "raw_rows_deleted": 0, "remaining_old_rows": 0}
        day_start = (oldest // 86400) * 86400
        end = min(day_start + 86400, cutoff)
        rows = con.execute(
            f"""SELECT * FROM observations WHERE {_RAW} AND event_time >= ?
            AND event_time < ? AND observed_at <= ? ORDER BY event_time,observed_at,id""",
            (day_start, end, now),
        ).fetchall()
        finalized = con.execute(
            """SELECT * FROM observations WHERE event_time > ? AND event_time <= ?
            AND json_extract(payload, '$.metadata.aggregation')='hourly_median'
            ORDER BY observed_at,id""",
            (day_start, end),
        ).fetchall()
        retained = {(row["fingerprint"], row["observed_at"]) for row in con.execute(
            """SELECT fingerprint,observed_at FROM observations WHERE event_time >= ? AND event_time < ?
            AND json_extract(payload, '$.metadata.aggregation')='late_raw_unmerged'""",
            (day_start, end),
        )}

    prior_hours = {}
    for row in finalized:
        aggregate = Observation(**json.loads(row["payload"]))
        original = json.dumps(aggregate.metadata["original_dimension"], sort_keys=True,
                              ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        key = (row["source"], row["asset"], row["metric"], row["data_mode"], original, row["event_time"])
        prior_hours[key] = aggregate
    groups = defaultdict(list)
    for row in rows:
        groups[_key(row)].append(row)
    aggregates, quarantine = [], []
    late_count = 0
    for (source, asset, metric, mode, dimension, hour_end), versions in groups.items():
        previous = prior_hours.get((source, asset, metric, mode, dimension, hour_end))
        if previous is not None:
            if previous.observed_at > now:
                raise ValueError("Compaction time cannot precede a finalized hour's availability.")
            late = [row for row in versions if not _covered(row, previous)]
            if not late:
                continue
            late_count += len(late)
            for row in late:
                raw = Observation(**json.loads(row["payload"]))
                preserved = replace(raw, metadata={
                    **raw.metadata,
                    "aggregation": "late_raw_unmerged",
                    "original_dimension": json.loads(dimension),
                    "retention_status": "partial_unmerged",
                    "original_coverage_complete": raw.metadata.get("coverage_complete"),
                    "coverage_complete": False,
                })
                fingerprint = content_hash({key: value for key, value in asdict(preserved).items()
                                            if key != "observed_at"})
                if (fingerprint, preserved.observed_at) not in retained:
                    quarantine.append(preserved)
            # Preserve the earlier statistic, not the late batch's median/count.
            if previous.metadata.get("retention_status") != "partial_unmerged":
                aggregates.append(replace(previous, observed_at=now, metadata={
                    **previous.metadata,
                    "retention_status": "partial_unmerged",
                    "coverage_complete": False,
                }))
            continue
        # Latest known version per native event, while all captured versions are deleted.
        latest = {row["event_time"]: row for row in versions}
        samples = [json.loads(row["payload"]) for row in latest.values()]
        values = [sample["value"] for sample in samples]
        flags = [sample.get("metadata", {}).get("coverage_complete") is True for sample in samples]
        first = samples[0]
        metadata = {
            "aggregation": "hourly_median",
            "min": min(values),
            "max": max(values),
            "sample_count": len(values),
            "sample_window_first": samples[0]["event_time"],
            "sample_window_last": samples[-1]["event_time"],
            "coverage_complete": all(flags),
            "coverage_fraction": sum(flags) / len(flags),
            # Separate aggregates from raw samples at the hour boundary in Store's key.
            "symbol": "retention-hourly:" + dimension,
            "original_dimension": json.loads(dimension),
            # IDs and cutoff identify exactly the raw snapshot, including old versions.
            "compaction_raw_max_id": max(row["id"] for row in versions),
            "compaction_available_at": now,
        }
        band = first.get("metadata", {}).get("band_percent")
        if band is not None:
            metadata["band_percent"] = band
        aggregates.append(Observation(source, asset, metric, hour_end, now, statistics.median(values),
                                      first["unit"], first.get("quote"), metadata, mode))

    # One Store transaction writes both preserved statistics and late evidence.
    store.put_observations([*aggregates, *quarantine])
    ids = [row["id"] for row in rows]
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
    # Preserve the existing response for ordinary compaction. Late evidence has
    # its own visible count, independent of the pending raw maintenance budget.
    result = {"aggregates_written": len(aggregates), "raw_rows_deleted": deleted,
              "remaining_old_rows": remaining}
    if late_count:
        result["late_rows_quarantined"] = late_count
    return result
