"""Small durable source/task clocks; collection remains synchronous and bounded."""

from __future__ import annotations

import re

from .storage import Store

_TRANSIENT = re.compile(
    r"RetryDeferred|RateLimitExceeded|DDoSProtection|RequestTimeout|NetworkError|"
    r"ExchangeNotAvailable|OnMaintenance|TransportError|ConnectError|ReadTimeout|"
    r"HTTP (?:429|5\d\d)\b"
)
_DEADLINE = re.compile(r"retry_at=(\d+(?:\.\d+)?)")


class Schedule:
    """Persist attempts separately from successes, before any run-history cleanup."""

    def __init__(self, store: Store, intervals: dict[str, int]):
        self.store, self.intervals = store, intervals
        with store.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS scheduler_state (
                source TEXT NOT NULL, task TEXT NOT NULL, last_attempt REAL NOT NULL,
                last_success REAL NOT NULL, failures INTEGER NOT NULL,
                next_due REAL NOT NULL, retry_kind TEXT NOT NULL,
                PRIMARY KEY(source,task))""")
        # Bootstrap old installations from all run groups, never a bounded global tail.
        # Existing durable state wins, including server-provided retry deadlines.
        baseline = {}
        # Legacy compound errors lack task attribution: conservatively apply them
        # to every named task. Precise task reports win ties at the same second.
        for report in sorted(store.scheduler_runs(),
                             key=lambda row: (row["observed_at"], "+" not in row["task"])):
            for task in report["task"].split("+"):
                if task not in intervals:
                    continue
                key = (report["source"], task)
                last_success = max(report["last_success"], baseline.get(key, {}).get("last_success", 0))
                baseline[key] = {**report, "last_success": last_success}
        existing = self.states()
        for (source, task), report in baseline.items():
            previous = existing.get((source, task), {})
            completed = report["observed_at"]
            # A precise success can correct an already-seeded ambiguous legacy
            # failure at the same second. Once reconciled, subsequent restarts
            # leave both success and failure counters unchanged.
            corrected_success = (completed == previous.get("last_attempt")
                                 and "+" not in report["task"] and not report["errors"]
                                 and previous.get("last_success", 0) < completed)
            if completed > previous.get("last_attempt", -1) or corrected_success:
                self.record(report, task, completed)

    def states(self) -> dict[tuple[str, str], dict]:
        with self.store.connection() as con:
            rows = con.execute("SELECT * FROM scheduler_state").fetchall()
        return {(row["source"], row["task"]): dict(row) for row in rows}

    def start(self, source: str, task: str, now: float) -> None:
        """Reserve a bounded retry delay before I/O, surviving an interrupted worker."""
        with self.store.connection() as con:
            previous = con.execute("SELECT * FROM scheduler_state WHERE source=? AND task=?",
                                   (source, task)).fetchone()
            failures = previous["failures"] if previous else 0
            delay = min(60 * 2 ** min(failures, 9), 21600)
            con.execute("""INSERT INTO scheduler_state VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source,task) DO UPDATE SET last_attempt=excluded.last_attempt,
                next_due=excluded.next_due,retry_kind=excluded.retry_kind""",
                        (source, task, now, previous["last_success"] if previous else 0,
                         failures, now + delay, "in_progress"))

    def record(self, report: dict, task: str, completed: float) -> None:
        source = report["source"]
        with self.store.connection() as con:
            previous = con.execute("SELECT * FROM scheduler_state WHERE source=? AND task=?",
                                   (source, task)).fetchone()
            # A compound old run can predate a single-task run seeded earlier.
            if previous and previous["last_attempt"] > completed:
                return
            errors = report.get("errors", [])
            failures = (previous["failures"] if previous else 0) + 1 if errors else 0
            pending_work = "pending_collection_work" in report.get("warnings", [])
            reported_success = 0 if pending_work else report.get("last_success", 0)
            last_success = max(previous["last_success"] if previous else 0, reported_success)
            last_success = last_success if errors or pending_work else completed
            kind, delay = "cadence", self.intervals[task]
            if errors:
                transient = any(_TRANSIENT.search(error) for error in errors)
                kind = "transient" if transient else "permanent_or_unknown"
                delay = min(60 * 2 ** min(failures - 1, 9), 21600) if transient else max(delay, 21600)
            deadlines = [float(value) for error in errors for value in _DEADLINE.findall(error)]
            if pending_work and (not errors or kind == "permanent_or_unknown"):
                kind, delay = "pending_collection_work", min(delay, 5)
            if task == "retention" and "pending_backlog" in report.get("warnings", []):
                delay = 60
            next_due = max(completed + delay, *deadlines) if deadlines else completed + delay
            con.execute("""INSERT INTO scheduler_state VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source,task) DO UPDATE SET last_attempt=excluded.last_attempt,
                last_success=excluded.last_success,failures=excluded.failures,
                next_due=excluded.next_due,retry_kind=excluded.retry_kind""",
                        (source, task, completed, last_success, failures, next_due, kind))
