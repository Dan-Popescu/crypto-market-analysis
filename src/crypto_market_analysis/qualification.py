"""Offline qualification of explicit native markets from one known-at snapshot.

Endpoint documentation, empirical coverage, runtime access, reuse rights and
French purchase eligibility are independent facts. No reader performs a probe,
downloads history, aliases quotes or combines source/data-mode histories.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .asset_universe import validate_asset
from .evidence import canonical_json, content_hash
from .source_catalog import SOURCE_PROFILES
from .storage import Store

NATIVE_MARKETS = ("BTC/EUR", "ETH/EUR", "BTC/USD", "ETH/USD")


def normalize_native_markets(markets):
    """Validate one bounded selection; catalogue support remains unverified."""
    if not isinstance(markets, (list, tuple)) or not 1 <= len(markets) <= 4:
        raise ValueError("Select one to four distinct native EUR/USD markets.")
    for market in markets:
        if not isinstance(market, str) or market.count("/") != 1:
            raise ValueError("Markets require a canonical asset/native quote pair.")
        asset, quote = market.split("/")
        validate_asset(asset)
        if quote not in ("EUR", "USD"):
            raise ValueError("Select native EUR/USD markets.")
    if len(set(markets)) != len(markets):
        raise ValueError("Select distinct native markets.")
    return list(markets)


class _ReferenceHash:
    """Hash exact ordered row references with constant Python memory."""

    def __init__(self):
        self.digest = hashlib.sha256()

    def step(self, *values):
        self.digest.update((canonical_json(values) + "\n").encode())

    def finalize(self):
        return self.digest.hexdigest()


class _CoverageSummary:
    """Accumulate ordered latest references, retaining only bounded details."""

    def __init__(self):
        self.count = self.gap_count = self.largest_gap = 0
        self.first = self.last = self.earliest_receipt = self.latest_receipt = None
        self.gaps, self.samples = [], []
        self.references = _ReferenceHash()

    def _gap(self, start, end):
        if end <= start:
            return
        hours = (end - start) // 3600
        self.gap_count += 1
        self.largest_gap = max(self.largest_gap, hours)
        if len(self.gaps) < self.gap_limit:
            self.gaps.append([start, end, hours])

    def step(self, row_id, fingerprint, opened, closed, observed, start, end, sample_limit, gap_limit):
        self.end, self.gap_limit = end, gap_limit
        self._gap(self.last if self.last is not None else start, opened)
        self.first = opened if self.first is None else self.first
        self.last = closed
        self.earliest_receipt = min(self.earliest_receipt, observed) if self.count else observed
        self.latest_receipt = max(self.latest_receipt, observed) if self.count else observed
        self.count += 1
        self.references.step(row_id, fingerprint, opened, closed, observed)
        if len(self.samples) < sample_limit:
            self.samples.append([row_id, opened, closed, observed, fingerprint])

    def finalize(self):
        self._gap(self.last, self.end)
        return canonical_json([self.count, self.first, self.last, self.earliest_receipt, self.latest_receipt,
                               self.gap_count, self.largest_gap, self.gaps, self.samples, self.references.finalize()])


def _integer(request, key, default, minimum=0, maximum=2_000_000):
    value = request.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}.")
    return value


def normalize_qualification_request(request, as_of):
    if not isinstance(request, dict):
        raise ValueError("Qualification requires a request object.")
    if type(as_of) is not int or as_of < 0:
        raise ValueError("as_of must be nonnegative UTC epoch seconds.")
    mode = request.get("data_mode", "live")
    if mode not in ("live", "import", "demo"):
        raise ValueError("Select live, import or demo data explicitly.")
    markets = normalize_native_markets(request.get("markets", list(NATIVE_MARKETS)))
    sources = request.get("sources", [])
    if (not isinstance(sources, list) or len(sources) > 16
            or any(not isinstance(s, str) or not s or len(s) > 64 for s in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("sources must be a list of at most 16 distinct explicit source IDs.")
    market_sources = request.get("market_sources", {})
    if not isinstance(market_sources, dict) or any(m not in markets for m in market_sources):
        raise ValueError("market_sources must map selected markets to explicit source lists.")
    selected = []
    for market in markets:
        names = market_sources.get(market, sources)
        if (not isinstance(names, list) or not names or len(names) > 16
                or any(not isinstance(s, str) or not s or len(s) > 64 for s in names)
                or len(set(names)) != len(names)):
            raise ValueError("Each market requires between 1 and 16 distinct explicit source IDs.")
        selected.extend((market, source) for source in names)
    days = _integer(request, "required_history_days", 730, 1, 20_000)
    end = _integer(request, "required_end", as_of // 3600 * 3600, 0, as_of)
    start = request.get("required_start", end - days * 86400)
    if type(start) is not int or start < 0 or start % 3600 or end % 3600 or start >= end:
        raise ValueError("Required history needs a nonempty aligned half-open UTC range within as_of.")
    if (end - start) // 3600 > 500_000:
        raise ValueError("Required history exceeds the 500000-hour qualification bound.")
    normalized = {
        "sources": list(sources), "market_sources": dict(market_sources), "markets": list(markets),
        "data_mode": mode, "required_history_days": days, "required_start": start, "required_end": end,
        "freshness_seconds": _integer(request, "freshness_seconds", 10800, 0, 10 * 365 * 86400),
        "max_gap_hours": _integer(request, "max_gap_hours", 0, 0, 500_000),
        "max_gap_details": _integer(request, "max_gap_details", 100, 0, 10_000),
        "max_evidence_rows": _integer(request, "max_evidence_rows", 256, 0, 10_000),
        "max_query_rows": _integer(request, "max_query_rows", 1_000_000, 1),
        "max_requests": _integer(request, "max_requests", 0, 0, 10_000),
    }
    if request.get("max_storage_bytes") is not None:
        normalized["max_storage_bytes"] = _integer(request, "max_storage_bytes", None, 1, 2**63 - 1)
    return normalized, selected


def _contract(source):
    profile = SOURCE_PROFILES.get(source)
    endpoint = next((e for e in profile.endpoints if e.id == "spot_candles"), None) if profile else None
    return {
        "capability": {
            "status": "documented_candidate" if endpoint else "unknown",
            "native_market_support": "unverified_by_documentation",
            "endpoint": asdict(endpoint) if endpoint else None,
            "documented_page_cap": endpoint.page_cap if endpoint else None,
            "documented_retention": endpoint.retention if endpoint else "unknown",
            "history_limitation": endpoint.history if endpoint else "unknown",
        },
        "rights": {"status": "intended_use_review_required", "description": endpoint.rights if endpoint else "unknown",
                   "source_url": endpoint.rights_url if endpoint else None,
                   "checked_at": endpoint.rights_checked_at if endpoint else None},
        "regulatory_eligibility": {
            "jurisdiction": "France", "evidence": profile.purchase_evidence if profile else {},
            "scope": "Legal entity and listed services only; independent of global data analysis.",
        },
    }


def _history_access(con, tables, source, asset, quote, mode, as_of):
    """Only explicit public adapter captures establish runtime access evidence."""
    result = {"status": "untested", "last_success": None, "last_failure": None,
              "scope": "This native source/market/data-mode only; documentation is not runtime evidence.",
              "evidence": None}
    if "local_records" in tables:
        imported = con.execute("""SELECT id,observed_at,payload FROM local_records
            WHERE kind='history_import' AND observed_at<=?
            AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.asset')=?
            AND json_extract(payload,'$.quote')=? AND json_extract(payload,'$.data_mode')=?
            AND json_extract(payload,'$.capture_evidence')='public_backfill_adapter'
            AND json_extract(payload,'$.validated_rows')>0
            ORDER BY observed_at DESC,id DESC LIMIT 1""", (as_of, source, asset, quote, mode)).fetchone()
        if imported:
            payload = json.loads(imported["payload"])
            result.update(status="success", last_success=imported["observed_at"],
                          evidence={"table": "local_records", "id": imported["id"],
                                    "capture_evidence": payload["capture_evidence"],
                                    "captured_at": payload.get("captured_at"),
                                    "response_sha256": payload.get("response_sha256"),
                                    "hash_basis": "normalized CCXT OHLCV rows; original provider body not retained"})
    if "history_captures" not in tables:
        return result
    row = con.execute("""SELECT id,materialized_at,
        json_extract(payload,'$.captured_at'),json_extract(payload,'$.capture_evidence'),
        json_extract(payload,'$.raw_response_sha256') FROM history_captures
        WHERE asset=? AND quote=? AND materialized_at<=?
        AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.data_mode')=?
        AND json_extract(payload,'$.capture_evidence')='public_rest_adapter'
        AND json_extract(payload,'$.acquisition')='public_rest'
        ORDER BY materialized_at DESC,id DESC LIMIT 1""", (asset, quote, as_of, source, mode)).fetchone()
    if row and (result["last_success"] is None or row[2] > result["last_success"]):
        result.update(status="success", last_success=row[2],
                      evidence={"table": "history_captures", "id": row[0], "materialized_at": row[1],
                                "capture_evidence": row[3], "raw_response_sha256": row[4]})
    return result


def _run_health(con, source, as_of):
    """Source-wide runs cannot prove any particular native market succeeded."""
    rows = con.execute("""SELECT id,observed_at,json_extract(payload,'$.status') status,
        json_extract(payload,'$.task') task FROM runs WHERE source=? AND observed_at<=?
        ORDER BY observed_at DESC,id DESC LIMIT 1""", (source, as_of)).fetchone()
    failure = con.execute("""SELECT id,observed_at,json_extract(payload,'$.status') status FROM runs
        WHERE source=? AND observed_at<=? AND json_extract(payload,'$.status') IN ('error','partial')
        ORDER BY observed_at DESC,id DESC LIMIT 1""", (source, as_of)).fetchone()
    return {"scope": "source_wide_market_and_mode_unknown", "latest_run": dict(rows) if rows else None,
            "last_failure": dict(failure) if failure else None,
            "market_success": "unknown"}


def _coverage(con, parameters, request, evidence_budget):
    """Filter time and knowledge before rank; never deserialize candle history."""
    start, end = request["required_start"], request["required_end"]
    where = """source=? AND asset=? AND quote=? AND data_mode=? AND timeframe='1h'
        AND open_time>=? AND open_time<? AND close_time<=? AND observed_at<=?
        AND open_time%3600=0 AND close_time=open_time+3600"""
    # Rank candidates once, then consume each ordered reference stream once.
    # The bounded aggregate replaces a second SQL window sort for gaps plus
    # repeated scans for stats and samples. It never reads candle payloads.
    con.create_aggregate("qualification_coverage", 9, _CoverageSummary)
    summary, versions, version_digest = con.execute(f"""WITH versions AS MATERIALIZED (
        SELECT id,open_time,close_time,observed_at,fingerprint,
            ROW_NUMBER() OVER(PARTITION BY open_time ORDER BY observed_at DESC,id DESC) rank
        FROM candles WHERE {where}),
        latest AS MATERIALIZED (SELECT * FROM versions WHERE rank=1)
        SELECT
            (SELECT qualification_coverage(id,fingerprint,open_time,close_time,observed_at,?,?,?,?) FROM
                (SELECT * FROM latest ORDER BY open_time,observed_at,id)),
            (SELECT COUNT(*) FROM versions),
            (SELECT qualification_refs(id,fingerprint,open_time,close_time,observed_at) FROM
                (SELECT * FROM versions ORDER BY open_time,observed_at,id))""",
        [*parameters, start, end, evidence_budget, request["max_gap_details"]]).fetchone()
    empty_digest = hashlib.sha256(b"").hexdigest()
    if summary is None:
        count = 0
        first = last = earliest_receipt = latest_receipt = None
        largest_gap = (end - start) // 3600
        gap_count = 1
        gap_rows = [[start, end, largest_gap]] if request["max_gap_details"] else []
        sample_rows, selected_digest = [], empty_digest
    else:
        (count, first, last, earliest_receipt, latest_receipt, gap_count, largest_gap,
         gap_rows, sample_rows, selected_digest) = json.loads(summary)
    gaps = [dict(zip(("start", "end", "hours"), values, strict=True)) for values in gap_rows]
    samples = [dict(zip(("id", "open_time", "close_time", "observed_at", "fingerprint"), values, strict=True))
               for values in sample_rows]
    # Row IDs alone are recyclable; fingerprints and receipt times bind references
    # to the actual ordered evidence consumed in this transaction.
    digests = {"selected": selected_digest, "versions": version_digest or empty_digest}
    expected = (end - start) // 3600
    return {
        "required_start": start, "required_end": end, "range_convention": "half_open_UTC_hourly",
        "expected_hours": expected, "available_hours": count, "missing_hours": expected - count,
        "coverage_fraction": count / expected, "available_start": first, "available_end": last,
        "versions_available": versions, "revision_rows": versions - count,
        "earliest_observed_at": earliest_receipt, "latest_observed_at": latest_receipt,
        "historical_known_at": "Stored observed_at is receipt availability, never historical publication time.",
        "gap_count": gap_count, "largest_gap_hours": largest_gap, "gap_ranges": gaps,
        "gap_details_omitted": gap_count - len(gaps), "coverage_complete": count == expected,
        "context_lookback_readiness": {
            "status": "not_established_by_hourly_coverage",
            "required_daily_closes_for_requested_day_return": request["required_history_days"] + 1,
            "note": "N days of hourly history do not establish an N-day return; N+1 daily closes are required.",
        },
        "calculation_truncated": False,
    }, {"table": "candles", "selection": "latest_observed_at_then_id_per_open_time",
        "row_reference_encoding": "canonical JSON [id,fingerprint,open_time,close_time,observed_at] plus LF",
        "selected_rows_sha256": digests["selected"], "version_rows_sha256": digests["versions"],
        "selected_rows": count, "version_rows": versions, "row_samples": samples,
        "samples_omitted": count - len(samples), "samples_truncated": count > len(samples)}


def qualify_sources(store: Store, request: dict, as_of: int) -> dict:
    """Qualify source/market pairs without network access or writes.

    ``sources`` and/or ``market_sources`` select explicit source IDs. ``markets``
    defaults to all four native BTC/ETH EUR/USD pairs. The default requirement is
    730 days ending at the last completed UTC hour; explicit aligned
    ``required_start``/``required_end`` can select a research window.

    ``max_query_rows`` bounds total candidate candle versions before ranking.
    ``max_evidence_rows`` bounds reference samples across the whole response;
    exact aggregates and content digests still cover all consumed rows. Lower
    storage/query budgets produce unavailable evidence, never a false pass.
    ``max_requests`` is reported but this read operation always makes zero calls.
    """
    rules, selected = normalize_qualification_request(request, as_of)
    start, end, mode = rules["required_start"], rules["required_end"], rules["data_mode"]
    results = []
    remaining_samples = rules["max_evidence_rows"]
    remaining_gaps = rules["max_gap_details"]
    remaining_rows = rules["max_query_rows"]
    with store.connection() as con:
        if not con.in_transaction:
            con.execute("BEGIN")
        con.create_aggregate("qualification_refs", 5, _ReferenceHash)
        watermark = {t: con.execute(f"SELECT COALESCE(MAX(id),0) FROM {t}").fetchone()[0]
                     for t in ("candles", "runs")}
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        database_bytes = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
        database_rows = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("candles", "observations", "runs", "events", "local_records")}
        storage_exceeded = database_bytes > rules.get("max_storage_bytes", 2**63 - 1)
        for market, source in selected:
            asset, quote = market.split("/")
            parameters = (source, asset, quote, mode, start, end, as_of, as_of)
            candidates = con.execute("""SELECT COUNT(*) FROM candles WHERE source=? AND asset=? AND quote=?
                AND data_mode=? AND timeframe='1h' AND open_time>=? AND open_time<?
                AND close_time<=? AND observed_at<=?""", parameters).fetchone()[0]
            reasons = []
            access = _history_access(con, tables, source, asset, quote, mode, as_of)
            health = _run_health(con, source, as_of)
            if storage_exceeded or candidates > remaining_rows:
                reason = "storage_budget_exceeded" if storage_exceeded else "query_row_budget_exceeded"
                coverage = {"required_start": start, "required_end": end, "expected_hours": (end-start)//3600,
                            "available_hours": None, "missing_hours": None, "coverage_complete": None,
                            "calculation_truncated": True, "candidate_version_rows": candidates}
                evidence = {"table": "candles", "selected_rows": 0, "row_samples": [],
                            "selection_not_performed": reason}
                freshness = {"status": "unknown", "last_success": None, "latest_closed_hour": None}
                status = "unavailable"
                reasons.append(reason)
            else:
                coverage, evidence = _coverage(con, parameters, {**rules, "max_gap_details": remaining_gaps},
                                               remaining_samples)
                remaining_rows -= candidates
                remaining_samples -= len(evidence["row_samples"])
                remaining_gaps -= len(coverage["gap_ranges"])
                last_receipt = coverage["latest_observed_at"]
                latest_closed = coverage["available_end"]
                receipt_age = as_of - last_receipt if last_receipt is not None else None
                close_age = as_of - latest_closed if latest_closed is not None else None
                fresh = (receipt_age is not None and receipt_age <= rules["freshness_seconds"]
                         and close_age <= rules["freshness_seconds"])
                freshness = {"status": "fresh" if fresh else "stale" if last_receipt is not None else "unknown",
                             "last_success": last_receipt, "success_basis": "stored_market_candle_receipt",
                             "latest_closed_hour": latest_closed, "receipt_age_seconds": receipt_age,
                             "close_age_seconds": close_age, "maximum_age_seconds": rules["freshness_seconds"],
                             "runtime_last_success": access["last_success"],
                             "runtime_last_failure": access["last_failure"]}
                if coverage["missing_hours"] > rules["max_gap_hours"]:
                    reasons.append("required_hourly_history_missing")
                if not fresh:
                    reasons.append("market_history_stale" if last_receipt is not None else "no_stored_market_history")
                if mode == "demo":
                    reasons.append("synthetic_data_cannot_qualify_a_real_source")
                status = ("unavailable" if not coverage["available_hours"] else "partial" if reasons else "pass")
            scope = {"source": source, "asset": asset, "quote": quote, "timeframe": "1h", "data_mode": mode,
                     "required_start": start, "required_end": end, "as_of": as_of,
                     "close_time_lte": as_of, "observed_at_lte": as_of, "watermark": watermark,
                     "hourly_integrity_filter": "open_time % 3600 = 0 AND close_time = open_time + 3600"}
            evidence["scope"] = scope
            evidence["reference"] = "sha256:" + content_hash(evidence)
            results.append({"market": market, "asset": asset, "quote": quote, "source": source,
                            "data_mode": mode, "status": status, "coverage": coverage, "freshness": freshness,
                            "source_access": access, "source_health": health, **_contract(source),
                            "evidence": evidence, "reasons": reasons})
    statuses = [r["status"] for r in results]
    status = ("pass" if all(s == "pass" for s in statuses) else
              "unavailable" if all(s == "unavailable" for s in statuses) else "partial")
    result = {"schema_version": 1, "as_of": as_of, "data_mode": mode, "status": status,
              "qualification_scope": "Empirical stored native hourly coverage; access, rights and eligibility remain separate.",
              "request": rules, "markets": results,
              "resources": {"requests_performed": 0, "request_budget": rules["max_requests"],
                            "candidate_versions_consumed": rules["max_query_rows"] - remaining_rows,
                            "query_row_budget": rules["max_query_rows"],
                            "evidence_rows_returned": rules["max_evidence_rows"] - remaining_samples,
                            "evidence_row_budget": rules["max_evidence_rows"],
                            "gap_details_returned": rules["max_gap_details"] - remaining_gaps,
                            "gap_detail_budget": rules["max_gap_details"],
                            "database_rows": database_rows,
                            "database_row_count_scope": "Physical rows in this transaction, including later-known records.",
                            "database_logical_bytes": database_bytes,
                            "storage_measurement": "SQLite snapshot logical pages; excludes WAL, SHM and external archives.",
                            "storage_budget_bytes": rules.get("max_storage_bytes"),
                            "storage_budget_exceeded": storage_exceeded, "local_retention_policy": "unknown"},
              "watermark": watermark}
    result["evidence_reference"] = "sha256:" + content_hash(result)
    return result
