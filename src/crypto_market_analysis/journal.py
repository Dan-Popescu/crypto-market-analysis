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
import time
from pathlib import Path

from .storage import Store

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 500_000


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


class AssessmentJournal:
    """Local write boundary; lookup/calculation adapters must not issue implicitly."""

    def __init__(self, store: Store):
        self.store = store
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
                CREATE TRIGGER IF NOT EXISTS assessment_journal_no_update
                BEFORE UPDATE ON assessment_journal BEGIN SELECT RAISE(ABORT,'Immutable assessment'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_journal_no_delete
                BEFORE DELETE ON assessment_journal BEGIN SELECT RAISE(ABORT,'Immutable assessment'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_outcomes_no_update
                BEFORE UPDATE ON assessment_outcomes BEGIN SELECT RAISE(ABORT,'Append-only outcome'); END;
                CREATE TRIGGER IF NOT EXISTS assessment_outcomes_no_delete
                BEFORE DELETE ON assessment_outcomes BEGIN SELECT RAISE(ABORT,'Append-only outcome'); END;
            """)

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
            con.execute(
                "INSERT INTO assessment_journal VALUES(?,?,?,?,?)",
                (record["assessment_id"], key, issued, content_hash, encoded),
            )
        return json.loads(encoded)

    def get(self, assessment_id: str) -> dict:
        identity = _key(assessment_id, "Assessment ID")
        with self.store.connection() as con:
            row = con.execute(
                "SELECT record FROM assessment_journal WHERE assessment_id=?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError("Assessment not found.")
        return json.loads(row["record"])

    def list(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Limit must be an integer from 1 through 1000.")
        with self.store.connection() as con:
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
            con.execute(
                "INSERT INTO assessment_outcomes(assessment_id,outcome_key,content_hash,record) VALUES(?,?,?,?)",
                (identity, key, content_hash, encoded),
            )
        return json.loads(encoded)

    def outcomes(self, assessment_id: str) -> list[dict]:
        identity = _key(assessment_id, "Assessment ID")
        self.get(identity)
        with self.store.connection() as con:
            rows = con.execute(
                "SELECT record FROM assessment_outcomes WHERE assessment_id=? ORDER BY sequence", (identity,)
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]
