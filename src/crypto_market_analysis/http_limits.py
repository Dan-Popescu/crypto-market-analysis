"""Shared, finite public HTTP attempt admission and monotonic deadlines.

These are cooperative transport limits, not process termination: arbitrary custom
clients, rate-limit sleeps and a peer trickling bytes cannot be forcibly stopped.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import RLock


@dataclass(frozen=True)
class HTTPPolicy:
    max_requests: int = 10_000
    deadline_seconds: float = 300.0

    def __post_init__(self):
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 1_000_000:
            raise ValueError("http_max_requests must be an integer between 1 and 1000000.")
        if (type(self.deadline_seconds) not in (int, float)
                or not math.isfinite(self.deadline_seconds)
                or not 0 < self.deadline_seconds <= 86400):
            raise ValueError("http_deadline_seconds must be finite and between 0 (exclusive) and 86400.")

    @classmethod
    def from_settings(cls, settings):
        return cls(getattr(settings, "http_max_requests", 10_000),
                   getattr(settings, "http_deadline_seconds", 300.0))


class HTTPBudgetExceeded(RuntimeError):
    """Sanitized refusal; no HTTP attempt has been sent for this exception."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"public HTTP budget exceeded: {reason}")


class HTTPBudget:
    def __init__(self, policy: HTTPPolicy | None = None):
        self.policy = policy or HTTPPolicy()
        if not isinstance(self.policy, HTTPPolicy):
            raise ValueError("policy must be an HTTPPolicy.")
        self.started = time.monotonic()
        self.deadline = self.started + self.policy.deadline_seconds
        self._lock = RLock()
        self.used_requests = 0
        self.refused_requests = 0
        self._unsupported = False
        self._observed = False
        self.reason = None

    def supported(self):
        with self._lock:
            self._observed = True

    def unsupported(self):
        with self._lock:
            self._unsupported = True

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        """Check between logical calls without inventing refused HTTP attempts."""
        with self._lock:
            reason = ("deadline" if self.remaining() <= 0 else
                      "request_limit" if self.used_requests >= self.policy.max_requests else None)
            if reason:
                self.reason = reason
                raise HTTPBudgetExceeded(reason)

    def admit(self) -> float:
        with self._lock:
            remaining = self.remaining()
            reason = ("deadline" if remaining <= 0 else
                      "request_limit" if self.used_requests >= self.policy.max_requests else None)
            if reason:
                self.reason = reason
                self.refused_requests += 1
                raise HTTPBudgetExceeded(reason)
            self.used_requests += 1
            return remaining

    def snapshot(self) -> dict:
        with self._lock:
            return {"scope": "shared_invocation", "max_requests": self.policy.max_requests,
                    "deadline_seconds": self.policy.deadline_seconds,
                    "used_requests": self.used_requests, "refused_requests": self.refused_requests,
                    "remaining_seconds": self.remaining(), "exceeded": self.reason,
                    "enforcement": ("partial" if self._unsupported and self._observed else
                                    "unsupported" if self._unsupported else
                                    "supported_transports" if self._observed else "unobserved"),
                    "deadline_basis": "monotonic; per-attempt timeouts; cooperative, not process termination"}


_current_budget: ContextVar[HTTPBudget | None] = ContextVar("public_http_budget", default=None)


def current_http_budget() -> HTTPBudget | None:
    return _current_budget.get()


@contextmanager
def http_budget_scope(policy: HTTPPolicy | None = None, *, budget: HTTPBudget | None = None):
    """Reuse an outer budget: nested capture scopes cannot reset its allowance."""
    active = current_http_budget()
    if active is not None:
        yield active
        return
    selected = budget if budget is not None else HTTPBudget(policy)
    if not isinstance(selected, HTTPBudget):
        raise ValueError("budget must be an HTTPBudget.")
    token = _current_budget.set(selected)
    try:
        yield selected
    finally:
        _current_budget.reset(token)


def bounded_http(function):
    @wraps(function)
    def bounded(settings, *args, **kwargs):
        with http_budget_scope(HTTPPolicy.from_settings(settings)) as budget:
            result = function(settings, *args, **kwargs)
            if isinstance(result, dict):
                result["http_limits"] = budget.snapshot()
            return result
    return bounded


def check_http_budget() -> None:
    if (budget := current_http_budget()) is not None:
        budget.check()


def bounded_public_operation(function):
    """Give standalone public operations a finite scope, preserving outer policy."""
    @wraps(function)
    def bounded(*args, **kwargs):
        with http_budget_scope() as budget:
            result = function(*args, **kwargs)
            if isinstance(result, dict):
                result["http_limits"] = budget.snapshot()
            return result
    return bounded


def bounded_engine_http(function):
    """Bound a public Engine operation by its configured shared policy."""
    @wraps(function)
    def bounded(engine, *args, **kwargs):
        with http_budget_scope(HTTPPolicy.from_settings(engine.settings)) as budget:
            result = function(engine, *args, **kwargs)
            if isinstance(result, dict):
                result["http_limits"] = budget.snapshot()
            return result
    return bounded
