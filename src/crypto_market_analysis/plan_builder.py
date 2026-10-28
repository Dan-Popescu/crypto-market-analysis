"""Optional local plan construction and price-free funding constraints.

The preview reserves cash for declared purchases without inventing fills, prices,
rounding refunds, or holdings. It is a conservative funding illustration, not an
account balance. Ledgerly remains authoritative for actual transactions.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta

from .plans import allocation_specs as allocation_specs
from .plans import policies_equivalent as policies_equivalent
from .plans import preview_plan_funding as preview_plan_funding
from .plans import required_cash as required_cash
from .plans import validate_config


def _utc_date(value: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("start_date must be an ISO timestamp with explicit UTC timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("start_date must be an ISO timestamp with explicit UTC timezone") from exc
    if result.utcoffset() != timedelta(0) or result.microsecond:
        raise ValueError("start_date must use UTC and whole seconds")
    return result.astimezone(UTC)


def construct_plan(request: dict) -> dict:
    """Expand a bounded recurrence into the existing normalized schema-1 plan.

    Request: explicit asset/quote/source, start_date (UTC ISO timestamp),
    recurrence {frequency, count, month_end_rule}, contribution_amount and
    purchase_amount (decimal strings), plus optional data_mode/fees/execution/
    plans. Monthly clip preserves the original day where possible; skip omits
    months lacking that day. Count is the number of generated occurrences.
    Defaults illustrate regular, half-now split, and fully deferred policies.
    """
    allowed = {"asset", "quote", "source", "data_mode", "start_date", "recurrence",
               "contribution_amount", "purchase_amount", "fees", "execution", "plans"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Unsupported constructor fields")
    start = _utc_date(request.get("start_date"))
    recurrence = request.get("recurrence")
    if not isinstance(recurrence, dict) or set(recurrence) - {"frequency", "count", "month_end_rule"}:
        raise ValueError("recurrence requires frequency and count")
    frequency, count = recurrence.get("frequency"), recurrence.get("count")
    if frequency not in ("daily", "weekly", "monthly"):
        raise ValueError("frequency must be daily, weekly or monthly")
    if type(count) is not int or not 1 <= count <= 181:
        raise ValueError("recurrence.count must be between 1 and 181")
    rule = recurrence.get("month_end_rule")
    if frequency == "monthly" and rule not in ("clip", "skip"):
        raise ValueError("Monthly recurrence requires month_end_rule clip or skip")
    if frequency != "monthly" and rule is not None:
        raise ValueError("month_end_rule applies only to monthly recurrence")
    times = []
    offset = 0
    while len(times) < count:
        try:
            if frequency == "monthly":
                year, month = divmod(start.year * 12 + start.month - 1 + offset, 12)
                month += 1
                last_day = calendar.monthrange(year, month)[1]
                candidate = start.replace(year=year, month=month, day=min(start.day, last_day))
            else:
                candidate = start + timedelta(days=offset * (7 if frequency == "weekly" else 1))
        except (ValueError, OverflowError) as exc:
            raise ValueError("Recurrence is outside the supported UTC range") from exc
        if candidate - start > timedelta(days=180):
            raise ValueError("Expanded recurrence must fit within 180 days")
        if frequency != "monthly" or rule == "clip" or candidate.day == start.day:
            times.append(int(candidate.timestamp()))
        offset += 1
    config = {"schema_version": 1, "asset": request.get("asset"), "quote": request.get("quote"),
              "source": request.get("source"), "data_mode": request.get("data_mode", "live"),
              "start_time": times[0],
              "contributions": [{"time": t, "amount": request.get("contribution_amount")} for t in times],
              "schedule": [{"time": t, "amount": request.get("purchase_amount")} for t in times],
              "fees": request.get("fees", {}), "execution": request.get("execution", {}),
              "plans": request.get("plans", {"split": {"initial_fraction": "0.5"},
                                              "deferred": {"fraction": "1"}})}
    return validate_config(config)
