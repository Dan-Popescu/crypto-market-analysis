"""Explicit local issuance with immutable originals and append-only outcomes.

Schema v1 is additive: constructing this journal creates two tables and update/
delete guards without changing existing market tables. Back up the SQLite store
(including WAL via SQLite's backup API) before migrations. Frozen consumed evidence
is stored in each original; market revision and retention never affect replay.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .storage import Store, database_table_names

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 500_000


# Projection expressions retain only contracts consumed by prospective/scorecard reducers.
# They execute inside SQLite so archived evidence never crosses the read boundary.
_VALIDATION_MARKET_FIELDS = ("source", "asset", "quote", "data_mode")
_ISSUE_PLAN_FIELDS = ("schema_version", "rule_version", "validation_mode", "config", "input_reference")
_OUTCOME_PLAN_FIELDS = (*_ISSUE_PLAN_FIELDS, *_VALIDATION_MARKET_FIELDS, "as_of", "replay")


def _json_fields(path, fields, *, column="record"):
    return ",".join(f"'{field}',json_extract({column},'{path}.{field}')" for field in fields)


_ISSUE_PROJECTION = "json_object(" + _json_fields("$", ("assessment_id", "issued_at", "content_hash")) + "," + (
    "'payload',json_object(" + _json_fields("$.payload", (
        "kind", "input_reference", "rules", "outcome_protocol", "historical_calculation",
        "validation_protocol_hash", "validation_predictions")) + ","
    "'windows',json_object(" + _json_fields("$.payload.windows", ("anchor_time", "anchor_price")) + ","
    "'windows',json(COALESCE((SELECT json_group_object(horizon.key,json("
    "(SELECT json_group_object(threshold.key,json('{}')) FROM json_each(horizon.value) AS threshold))) "
    "FROM json_each(record,'$.payload.windows.windows') AS horizon),'{}'))),"
    "'plan',CASE WHEN json_type(record,'$.payload.plan')='object' THEN json_object("
    + _json_fields("$.payload.plan", _ISSUE_PLAN_FIELDS) + ") ELSE NULL END))"
)
_WINDOW_FIELDS = ("horizon_days", "threshold", "as_of", "issued_at", "anchor_time", "anchor_price", "window_end")
_WINDOW_EVENTS = ("endpoint_below_anchor", "below_anchor_touch", "observable_endpoint")
_OUTCOME_WINDOWS = (
    "json(COALESCE((SELECT json_group_array(json_object(" + _json_fields("$", _WINDOW_FIELDS, column="window.value")
    + "," + ",".join("'" + event + "',json_object(" + _json_fields("$." + event, ("state", "convention"),
                                                                                column="window.value") + ")"
                      for event in _WINDOW_EVENTS)
    + ")) FROM json_each(record,'$.payload.windows') AS window),'[]'))"
)
_OUTCOME_PROJECTION = "json_object(" + _json_fields("$", ("assessment_id", "observed_at", "content_hash")) + "," + (
    "'payload',json_object(" + _json_fields("$.payload", ("as_of", "input_reference", "original_input_reference"))
    + ",'windows'," + _OUTCOME_WINDOWS + ","
    "'plan',CASE WHEN json_type(record,'$.payload.plan')='object' THEN json_object("
    + _json_fields("$.payload.plan", _OUTCOME_PLAN_FIELDS) + ",'plans',json_object("
    + ",".join(f"'{family}',json_object('terminals',json_extract(record,'$.payload.plan.plans.{family}.terminals'))"
               for family in ("regular", "split", "deferred")) + ")) ELSE NULL END))"
)
_VALIDATION_PROJECTION_BYTES = 2 * 1024 * 1024
_VALIDATION_RETURNED_BYTES = 64 * 1024 * 1024


def _canonical(value: object) -> str:
    """Reject coercions, nonfinite numbers and unbounded JSON before persistence."""
    remaining = MAX_JSON_NODES

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON exceeds the depth or item limit.")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or not key or len(key) > 1024:
                    raise ValueError("JSON object keys must be nonempty strings of at most 1024 characters.")
                visit(child, depth + 1)
            return
        raise ValueError("Expected finite JSON values without type coercion.")

    try:
        visit(value, 0)
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("JSON exceeds the 32 MiB record limit.")
        return encoded
    except (RecursionError, UnicodeError, OverflowError) as exc:
        raise ValueError("Invalid or oversized JSON.") from exc


def _digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _key(value: str, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 UTF-8 bytes.")
    return value


def _timestamp(value: int | None) -> int:
    if value is None:
        return int(time.time())
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ValueError("Timestamp must be a nonnegative 64-bit integer UTC epoch second.")
    return value


def _provenance(payload: dict) -> dict:
    package = Path(__file__).parent
    code = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        code.update(path.relative_to(package).as_posix().encode())
        code.update(b"\0")
        code.update(path.read_bytes())
    lock = package.parent.parent / "uv.lock"
    config, rules = payload.get("config", {}), payload.get("rules", {})
    return {
        "code_revision": payload.get("code_revision"),
        "code_content_hash": code.hexdigest(),
        "dependency_lock_hash": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None,
        "config": config,
        "config_hash": _digest(_canonical(config)),
        "rules": rules,
        "rules_hash": _digest(_canonical(rules)),
    }


@dataclass(frozen=True)
class JournalSelection:
    """One bounded, evidence-free read snapshot for prospective calculation."""

    as_of: int
    records: list[dict]
    outcomes: dict[str, list[dict]]
    selection: dict


class AssessmentJournal:
    """Own immutable issuance, append-only outcomes and their SQLite read lifecycle."""

    def __init__(self, store: Store, *, initialize: bool = True):
        self.store = store
        if not initialize:
            return
        with store.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS assessment_journal (
                    assessment_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    issued_at INTEGER NOT NULL, content_hash TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assessment_outcomes (
                    sequence INTEGER PRIMARY KEY, assessment_id TEXT NOT NULL,
                    outcome_key TEXT NOT NULL, content_hash TEXT NOT NULL, record TEXT NOT NULL,
                    UNIQUE(assessment_id,outcome_key),
                    FOREIGN KEY(assessment_id) REFERENCES assessment_journal(assessment_id)
                );
                CREATE TABLE IF NOT EXISTS assessment_validation_issues (
                    assessment_id TEXT PRIMARY KEY REFERENCES assessment_journal(assessment_id),
                    issued_at INTEGER NOT NULL, source TEXT, asset TEXT, quote TEXT, data_mode TEXT,
                    dataset_lineage TEXT, record TEXT NOT NULL
                        CHECK(length(CAST(record AS BLOB))<=2097152)
                );
                CREATE TABLE IF NOT EXISTS assessment_validation_outcomes (
                    sequence INTEGER PRIMARY KEY REFERENCES assessment_outcomes(sequence),
                    assessment_id TEXT NOT NULL, observed_at INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    record TEXT NOT NULL CHECK(length(CAST(record AS BLOB))<=2097152)
                );
                CREATE TABLE IF NOT EXISTS assessment_validation_migration (
                    kind TEXT PRIMARY KEY, cursor INTEGER NOT NULL, through_rowid INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS assessment_validation_market ON assessment_validation_issues (
                    source,asset,quote,data_mode,dataset_lineage,issued_at,assessment_id
                );
                CREATE INDEX IF NOT EXISTS assessment_outcome_observed ON assessment_validation_outcomes (
                    assessment_id,observed_at,content_hash
                );
                CREATE TRIGGER IF NOT EXISTS assessment_validation_issues_no_update
                BEFORE UPDATE ON assessment_validation_issues BEGIN SELECT RAISE(ABORT,'Immutable projection'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_validation_issues_no_delete
                BEFORE DELETE ON assessment_validation_issues BEGIN SELECT RAISE(ABORT,'Immutable projection'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_validation_outcomes_no_update
                BEFORE UPDATE ON assessment_validation_outcomes BEGIN SELECT RAISE(ABORT,'Immutable projection'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_validation_outcomes_no_delete
                BEFORE DELETE ON assessment_validation_outcomes BEGIN SELECT RAISE(ABORT,'Immutable projection'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_journal_no_update
                BEFORE UPDATE ON assessment_journal BEGIN SELECT RAISE(ABORT,'Immutable assessment'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_journal_no_delete
                BEFORE DELETE ON assessment_journal BEGIN SELECT RAISE(ABORT,'Immutable assessment'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_outcomes_no_update
                BEFORE UPDATE ON assessment_outcomes BEGIN SELECT RAISE(ABORT,'Append-only outcome'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_outcomes_no_delete
                BEFORE DELETE ON assessment_outcomes BEGIN SELECT RAISE(ABORT,'Append-only outcome'); END;
            """)
            con.execute("BEGIN IMMEDIATE")
            self._migrate_validation_batch(con, 100, _VALIDATION_RETURNED_BYTES)

    @classmethod
    def open_existing(cls, store: Store) -> AssessmentJournal:
        """Access existing records without creating a database, schema or guards."""
        return cls(store, initialize=False)

    @contextmanager
    def _read_connection(self):
        if not self.store.path.is_file():
            yield None
            return
        # mode=ro also prevents SQLite from creating a file if it disappears
        # between the existence check and opening the connection.
        con = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assessment_journal'"
            ).fetchone()
            yield con if exists else None
        finally:
            con.close()

    def original_for_request(self, idempotency_key: str, request_hash: str) -> dict | None:
        """Recover the complete immutable original, or reject a reused request key."""
        key = _key(idempotency_key, "Idempotency key")
        with self._read_connection() as con:
            row = con.execute(
                "SELECT record FROM assessment_journal WHERE idempotency_key=?", (key,)
            ).fetchone() if con is not None else None
        if row is None:
            return None
        original = json.loads(row["record"])
        if original["payload"].get("request_hash") != request_hash:
            raise ValueError("Idempotency key already belongs to a different request.")
        return original

    def summary_inputs(self, as_of: int | None = None, limit: int = 20) -> JournalSelection:
        """Read originals and bounded outcomes at one cutoff in one SQLite snapshot.

        Evidence is removed inside SQLite before records cross this boundary.
        Legacy originals without availability metadata remain unverifiable in the
        prospective reducer; summary reads never restore their evidence archives.
        """
        now = int(time.time()) if as_of is None else as_of
        if type(now) is not int or now < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Use a nonnegative UTC cutoff and a journal limit from 1 through 100.")
        records, outcomes, truncated, total = [], {}, [], 0
        with self._read_connection() as con:
            if con is not None:
                total = con.execute(
                    "SELECT COUNT(*) FROM assessment_journal WHERE issued_at<=?", (now,)
                ).fetchone()[0]
                rows = con.execute(
                    "SELECT json_remove(record,'$.evidence') FROM assessment_journal WHERE issued_at<=? "
                    "ORDER BY issued_at DESC,assessment_id LIMIT ?", (now, limit)
                ).fetchall()
                records = [json.loads(row[0]) for row in rows]
                for record in records:
                    identity = record["assessment_id"]
                    values = con.execute(
                        "SELECT record FROM assessment_outcomes WHERE assessment_id=? "
                        "AND json_extract(record,'$.observed_at')<=? ORDER BY sequence LIMIT 257",
                        (identity, now),
                    ).fetchall()
                    outcomes[identity] = [json.loads(row[0]) for row in values[:256]]
                    if len(values) > 256:
                        truncated.append(identity)
        selection = {"scope": "most_recent_issuances_at_or_before_cutoff", "limit": limit,
                     "available_records": total, "records_truncated": total > limit,
                     "outcome_limit_per_record": 256, "outcomes_truncated": truncated}
        return JournalSelection(now, records, outcomes, selection)

    @contextmanager
    def _validation_read_connection(self):
        """Budget archive inputs separately from the small returned projections."""
        if not self.store.path.is_file():
            yield None
            return
        con = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        started, steps, returned, exhausted_reason = time.monotonic(), 0, 0, None

        def exhausted():
            nonlocal steps, exhausted_reason
            steps += 1000
            if steps > 50_000_000:
                exhausted_reason = "50-million SQLite VM-step budget"
            elif time.monotonic() - started > 15:
                exhausted_reason = "15-second journal read deadline"
            return exhausted_reason is not None

        def bounded_row(cursor, values):
            nonlocal returned
            sizes = [len(value.encode("utf-8")) if isinstance(value, str) else len(value)
                     if isinstance(value, bytes) else 8 for value in values]
            if any(size > _VALIDATION_PROJECTION_BYTES for size in sizes):
                raise ValueError("Journal validation projection exceeds its 2 MiB returned-value budget")
            returned += sum(sizes)
            if returned > _VALIDATION_RETURNED_BYTES:
                raise ValueError("Journal validation exceeds its 64 MiB returned-data budget")
            return sqlite3.Row(cursor, values)

        try:
            con.row_factory = bounded_row
            # Existing accepted archives may be 32 MiB. Only projected metadata
            # can leave SQLite, under the separate row/value budgets above.
            con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_RECORD_BYTES + 4096)
            con.set_progress_handler(exhausted, 1000)
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            yield con
            if time.monotonic() - started > 15:
                raise ValueError("Journal validation exceeded its 15-second read lifetime budget")
        except sqlite3.DatabaseError as exc:
            if exhausted_reason:
                raise ValueError(f"Journal validation exceeded its {exhausted_reason}; narrow the request") from exc
            raise ValueError(f"Journal validation read failed without migration: {exc}") from exc
        finally:
            con.close()

    @staticmethod
    def _insert_validation_projection(con, kind, rowid):
        if kind == "issue":
            fields = ",".join(f"json_extract(record,'$.payload.input_reference.{key}')"
                              for key in _VALIDATION_MARKET_FIELDS)
            con.execute(
                "INSERT INTO assessment_validation_issues "
                "SELECT assessment_id,issued_at," + fields + ","
                "json_extract(record,'$.payload.input_reference.dataset_lineage')," + _ISSUE_PROJECTION
                + " FROM assessment_journal WHERE rowid=? ON CONFLICT(assessment_id) DO NOTHING", (rowid,))
        else:
            con.execute(
                "INSERT INTO assessment_validation_outcomes "
                "SELECT sequence,assessment_id,json_extract(record,'$.observed_at'),content_hash,"
                + _OUTCOME_PROJECTION + " FROM assessment_outcomes WHERE sequence=? "
                "ON CONFLICT(sequence) DO NOTHING", (rowid,))

    def _migrate_validation_batch(self, con, record_limit, byte_budget):
        started, steps = time.monotonic(), 0
        exhausted_reason = None

        def exhausted():
            nonlocal steps, exhausted_reason
            steps += 1000
            if steps > 50_000_000 or time.monotonic() - started > 15:
                exhausted_reason = "50-million VM-step or 15-second migration budget"
            return exhausted_reason is not None

        result = {"migrated_issues": 0, "migrated_outcomes": 0, "input_bytes": 0}
        con.set_progress_handler(exhausted, 1000)
        try:
            for kind, table in (("issue", "assessment_journal"), ("outcome", "assessment_outcomes")):
                con.execute("INSERT OR IGNORE INTO assessment_validation_migration "
                            f"SELECT ?,0,COALESCE(MAX(rowid),0) FROM {table}", (kind,))
                cursor, through = con.execute(
                    "SELECT cursor,through_rowid FROM assessment_validation_migration WHERE kind=?", (kind,)
                ).fetchone()
                remaining = record_limit - result["migrated_issues"] - result["migrated_outcomes"]
                if remaining <= 0:
                    continue
                rows = con.execute(f"SELECT rowid,length(CAST(record AS BLOB)) FROM {table} "
                                   "WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
                                   (cursor, through, remaining)).fetchall()
                for rowid, size in rows:
                    if result["input_bytes"] + size > byte_budget:
                        break
                    self.store.check_write_budget(con, min(size, _VALIDATION_PROJECTION_BYTES))
                    self._insert_validation_projection(con, kind, rowid)
                    result["migrated_issues" if kind == "issue" else "migrated_outcomes"] += 1
                    result["input_bytes"] += size
                    cursor = rowid
                if not rows:
                    cursor = through
                con.execute("UPDATE assessment_validation_migration SET cursor=? WHERE kind=?", (cursor, kind))
            result["complete"] = not con.execute(
                "SELECT 1 FROM assessment_validation_migration WHERE cursor<through_rowid LIMIT 1").fetchone()
            if time.monotonic() - started > 15:
                raise ValueError("Journal projection migration exceeded its 15-second lifetime budget")
        except sqlite3.OperationalError as exc:
            if exhausted_reason:
                raise ValueError(f"Journal projection exceeded its {exhausted_reason}") from exc
            raise
        finally:
            con.set_progress_handler(None, 0)
        return result

    def migrate_validation_projections(self, *, record_limit=100, byte_budget=64 * 1024 * 1024):
        """Explicit writer-only continuation of a bounded projection backfill.

        Initialization creates the projection schema and migrates one batch.
        Repeat this method until complete for larger legacy journals. Read-only
        evaluation works through its bounded legacy projection during migration.
        """
        if (type(record_limit) is not int or not 1 <= record_limit <= 100
                or type(byte_budget) is not int or not MAX_RECORD_BYTES <= byte_budget <= _VALIDATION_RETURNED_BYTES):
            raise ValueError("Use 1 through 100 records and a 32 through 64 MiB migration input budget")
        if getattr(self.store, "read_only", False):
            raise ValueError("Projection migration requires an explicitly initialized writable journal")
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            return self._migrate_validation_batch(con, record_limit, byte_budget)

    def validation_records(self, cohorts: list[dict], *, starts_at: int, ends_at: int, as_of: int,
                           origins_per_cohort: int = 100, record_budget: int = 1000) -> JournalSelection:
        """Select bounded market cohorts in one resource-limited read snapshot.

        Only explicit journal initialization adds indexes. This read removes
        evidence inside SQLite and preserves one overflow sentinel for append
        truncation. Missing stores remain absent; legacy schemas are never upgraded.
        The journal session bounds archive inputs, projected bytes, VM work and elapsed time.
        """
        start, end, now = (_timestamp(value) for value in (starts_at, ends_at, as_of))
        if any(value is None for value in (starts_at, ends_at, as_of)) or start >= end:
            raise ValueError("Declare an increasing issuance range and explicit UTC cutoff")
        if (not isinstance(cohorts, list) or len(cohorts) > 100
                or type(origins_per_cohort) is not int or not 1 <= origins_per_cohort <= 100
                or type(record_budget) is not int or not 1 <= record_budget <= 1000
                or len(cohorts) * origins_per_cohort > record_budget):
            raise ValueError("Use at most 100 cohorts, 100 origins per cohort and a 1000-record budget")
        market_fields = ("source", "asset", "quote", "data_mode")
        seen = set()
        for cohort in cohorts:
            if not isinstance(cohort, dict):
                raise ValueError("Expected explicit journal cohort selectors")
            for key in ("cohort_id", *market_fields):
                _key(cohort.get(key), key)
            if "dataset_lineage" not in cohort or (cohort["dataset_lineage"] is not None
                    and not isinstance(cohort["dataset_lineage"], dict)):
                raise ValueError("Declare an object or null dataset lineage")
            if cohort["cohort_id"] in seen:
                raise ValueError("Cohort IDs must be unique")
            seen.add(cohort["cohort_id"])
        records, outcomes, truncated, truncated_outcomes = [], {}, [], []
        projection_mode = "missing_store"
        with self._validation_read_connection() as con:
            tables = database_table_names(con)
            if {"assessment_journal", "assessment_outcomes"} <= tables:
                projected = {"assessment_validation_issues", "assessment_validation_outcomes"} <= tables and bool(
                    con.execute("SELECT (SELECT COUNT(*) FROM assessment_journal)="
                                "(SELECT COUNT(*) FROM assessment_validation_issues) AND "
                                "(SELECT COUNT(*) FROM assessment_outcomes)="
                                "(SELECT COUNT(*) FROM assessment_validation_outcomes)").fetchone()[0])
                projection_mode = "persisted" if projected else "bounded_legacy_projection"
                for cohort in cohorts:
                    if projected:
                        filters = " AND ".join(f"{key}=?" for key in market_fields)
                        issue_sql = (
                            "SELECT record FROM assessment_validation_issues WHERE "
                            "json_extract(record,'$.payload.kind')='accumulation_assessment' "
                            f"AND issued_at>=? AND issued_at<? AND issued_at<=? AND {filters} "
                            "AND dataset_lineage IS json_extract(?,'$') ORDER BY issued_at,assessment_id LIMIT ?")
                        outcome_sql = ("SELECT record FROM assessment_validation_outcomes WHERE assessment_id=? "
                                       "AND observed_at<=? ORDER BY observed_at,content_hash LIMIT 257")
                    else:
                        filters = " AND ".join(
                            f"json_extract(record,'$.payload.input_reference.{key}')=?" for key in market_fields)
                        issue_sql = ("SELECT " + _ISSUE_PROJECTION + " FROM assessment_journal "
                                     "WHERE json_extract(record,'$.payload.kind')='accumulation_assessment' "
                                     f"AND issued_at>=? AND issued_at<? AND issued_at<=? AND {filters} "
                                     "AND json_extract(record,'$.payload.input_reference.dataset_lineage') "
                                     "IS json_extract(?,'$') ORDER BY issued_at,assessment_id LIMIT ?")
                        outcome_sql = ("SELECT " + _OUTCOME_PROJECTION + " FROM assessment_outcomes "
                                       "WHERE assessment_id=? AND json_extract(record,'$.observed_at')<=? "
                                       "ORDER BY json_extract(record,'$.observed_at'),content_hash LIMIT 257")
                    rows = con.execute(issue_sql, (
                        start, end, now, *(cohort[key] for key in market_fields),
                        _canonical(cohort["dataset_lineage"]), origins_per_cohort + 1)).fetchall()
                    if len(rows) > origins_per_cohort:
                        truncated.append(cohort["cohort_id"])
                    for row in rows[:origins_per_cohort]:
                        record = json.loads(row[0])
                        records.append(record)
                        identity = record["assessment_id"]
                        appends = con.execute(outcome_sql, (identity, now)).fetchall()
                        outcomes[identity] = [json.loads(append[0]) for append in appends]
                        if len(appends) > 256:
                            truncated_outcomes.append(identity)
        selection = {"scope": "earliest_issuances_per_declared_market_cohort", "origins_per_cohort": origins_per_cohort,
                     "record_budget": record_budget, "selected_records": len(records),
                     "origins_truncated": truncated, "outcomes_truncated": truncated_outcomes,
                     "origin_order": "earliest_issued_at_then_assessment_id", "evidence_loaded": False,
                     "projection_mode": projection_mode,
                     "resource_limits": {"read_session_seconds": 15, "sqlite_vm_steps": 50_000_000,
                                         "returned_bytes": _VALIDATION_RETURNED_BYTES, "single_value_bytes": _VALIDATION_PROJECTION_BYTES,
                                         "archive_input_bytes": MAX_RECORD_BYTES}}
        return JournalSelection(now, records, outcomes, selection)

    def issue(
        self, idempotency_key: str, payload: dict, evidence: dict, issued_at: int | None = None
    ) -> dict:
        key = _key(idempotency_key, "Idempotency key")
        issued = _timestamp(issued_at)
        if type(payload) is not dict or type(evidence) is not dict:
            raise ValueError("Payload and frozen consumed evidence must be JSON objects.")
        content = _canonical({"payload": payload, "evidence": evidence})
        content_hash = _digest(content)
        # Retry matching uses canonical content, never a newly sampled wall clock.
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute(
                "SELECT content_hash,record FROM assessment_journal WHERE idempotency_key=?", (key,)
            ).fetchone()
            if prior:
                if prior["content_hash"] != content_hash:
                    raise ValueError("Idempotency key already belongs to different assessment content.")
                return json.loads(prior["record"])
            record = {
                "schema_version": SCHEMA_VERSION,
                "assessment_id": "assessment_" + _digest(_canonical([key, content_hash])),
                "idempotency_key": key,
                "issued_at": issued,
                "content_hash": content_hash,
                **json.loads(content),
                "provenance": _provenance(payload),
            }
            encoded = _canonical(record)
            self.store.check_write_budget(con, len(encoded.encode()) * 2)
            rowid = con.execute(
                "INSERT INTO assessment_journal VALUES(?,?,?,?,?)",
                (record["assessment_id"], key, issued, content_hash, encoded),
            ).lastrowid
            self._insert_validation_projection(con, "issue", rowid)
        return json.loads(encoded)

    def get(self, assessment_id: str) -> dict:
        identity = _key(assessment_id, "Assessment ID")
        with self._read_connection() as con:
            if con is None:
                raise KeyError("Assessment not found; no analyses have been issued.")
            row = con.execute(
                "SELECT record FROM assessment_journal WHERE assessment_id=?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError("Assessment not found.")
        return json.loads(row["record"])

    def list(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Limit must be an integer from 1 through 1000.")
        with self._read_connection() as con:
            if con is None:
                return []
            rows = con.execute(
                "SELECT record FROM assessment_journal ORDER BY issued_at DESC,assessment_id LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    def append_outcome(
        self, assessment_id: str, outcome_key: str, payload: dict, observed_at: int | None = None
    ) -> dict:
        identity, key = _key(assessment_id, "Assessment ID"), _key(outcome_key, "Outcome key")
        observed = _timestamp(observed_at)
        if type(payload) is not dict:
            raise ValueError("Outcome payload must be a JSON object.")
        encoded_payload = _canonical(payload)
        content_hash = _digest(encoded_payload)
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            original = con.execute(
                "SELECT issued_at FROM assessment_journal WHERE assessment_id=?", (identity,)
            ).fetchone()
            if original is None:
                raise KeyError("Assessment not found.")
            prior = con.execute(
                "SELECT content_hash,record FROM assessment_outcomes WHERE assessment_id=? AND outcome_key=?",
                (identity, key),
            ).fetchone()
            if prior:
                if prior["content_hash"] != content_hash:
                    raise ValueError("Outcome key already belongs to different outcome content.")
                return json.loads(prior["record"])
            if observed < original["issued_at"]:
                raise ValueError("An outcome cannot be observed before assessment issuance.")
            record = {
                "schema_version": SCHEMA_VERSION, "assessment_id": identity, "outcome_key": key,
                "observed_at": observed, "content_hash": content_hash, "payload": json.loads(encoded_payload),
            }
            encoded = _canonical(record)
            self.store.check_write_budget(con, len(encoded.encode()) * 2)
            rowid = con.execute(
                "INSERT INTO assessment_outcomes(assessment_id,outcome_key,content_hash,record) VALUES(?,?,?,?)",
                (identity, key, content_hash, encoded),
            ).lastrowid
            self._insert_validation_projection(con, "outcome", rowid)
        return json.loads(encoded)

    def outcomes(self, assessment_id: str) -> list[dict]:
        identity = _key(assessment_id, "Assessment ID")
        with self._read_connection() as con:
            if con is None:
                raise KeyError("Assessment not found; no analyses have been issued.")
            if con.execute(
                "SELECT 1 FROM assessment_journal WHERE assessment_id=?", (identity,)
            ).fetchone() is None:
                raise KeyError("Assessment not found.")
            rows = con.execute(
                "SELECT record FROM assessment_outcomes WHERE assessment_id=? ORDER BY sequence", (identity,)
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]
