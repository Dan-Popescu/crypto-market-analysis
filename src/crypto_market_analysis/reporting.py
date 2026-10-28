"""Deterministic accumulation reports and bounded structured input validation."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext


def bounded_object(value: object, name: str = "configuration") -> dict:
    """Require finite, bounded JSON objects before passing them to calculations."""
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 20000 or depth > 20:
            raise ValueError(f"{name} exceeds the JSON structure limit.")
        if isinstance(item, dict):
            if len(item) > 2000 or any(not isinstance(k, str) for k in item):
                raise ValueError(f"{name} requires bounded string-key objects.")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 2000:
                raise ValueError(f"{name} arrays are limited to 2000 entries.")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{name} requires finite JSON numbers.")
        elif isinstance(item, str):
            if len(item) > 200000:
                raise ValueError(f"{name} contains an oversized string.")
        elif item is not None and not isinstance(item, (int, bool)):
            raise ValueError(f"{name} must contain JSON values only.")

    visit(value)
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} requires finite JSON values.") from exc
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ValueError(f"{name} exceeds the 2 MB limit.")
    return value


def bounded_integers(values, name: str, maximum: int, count: int) -> list[int] | None:
    if values is None:
        return None
    if not isinstance(values, list) or not 1 <= len(values) <= count:
        raise ValueError(f"{name} requires between 1 and {count} entries.")
    if any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= maximum for v in values):
        raise ValueError(f"{name} requires integers from 1 to {maximum}.")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} requires unique entries.")
    return values


def bounded_thresholds(values) -> list[float] | None:
    if values is None:
        return None
    if not isinstance(values, list) or not 1 <= len(values) <= 12:
        raise ValueError("thresholds requires between 1 and 12 entries.")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1 for v in values):
        raise ValueError("thresholds requires finite fractions strictly between zero and one.")
    if len(set(values)) != len(values):
        raise ValueError("thresholds requires unique entries.")
    return values


def bounded_origins(values) -> list[int] | None:
    if values is None:
        return None
    if not isinstance(values, list) or not 1 <= len(values) <= 24:
        raise ValueError("origins requires between 1 and 24 timestamps.")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        raise ValueError("origins requires integer UTC timestamps.")
    if len(set(values)) != len(values):
        raise ValueError("origins requires unique timestamps.")
    return values


def _display(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return str(value)


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _date(value) -> str:
    if type(value) is int:
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return _display(value)


def _percent(value) -> str:
    if value is None:
        return "unknown"
    try:
        with localcontext() as context:
            context.prec = max(28, len(str(value)) + 4)
            number = format(Decimal(str(value)) * 100, "f")
            return number.rstrip("0").rstrip(".") + "%" if "." in number else number + "%"
    except InvalidOperation:
        return _display(value)


def _amount(value, quote: str) -> str:
    return f"{_display(value)} {quote}" if value is not None else "unknown"


def _cell(value) -> str:
    return _display(value).replace("|", "\\|").replace("\n", "<br>")


def _table(lines: list[str], headers, rows) -> None:
    lines.extend(["", "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"])
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    lines.append("")


def _value(key: str, value, quote: str):
    if key.endswith(("_at", "_time")) or key in (
        "time",
        "origin",
        "deadline",
        "created",
        "held_out_start",
        "start",
        "end",
    ):
        return _date(value)
    if key == "requested_origins" and isinstance(value, list):
        return ", ".join(_date(item) for item in value) or "none"
    if key in (
        "decline",
        "fraction",
        "initial_fraction",
        "rate",
        "underperformance_fraction",
        "maximum_drawdown",
    ):
        return _percent(value)
    if key in (
        "target",
        "target_price",
        "terminal_wealth_difference",
        "reserve_difference",
        "acquisition_cost_difference",
        "funded_from_current_cash",
        "funded_from_future_contributions",
        "locked_cash",
        "cap_overflow_immediate_cash",
        "free_cash_after",
        "locked_cash_after",
        "amount_spent",
        "fees_quote",
        "new_purchases_all_in_cost",
        "allocated_cash",
        "required_cash",
        "requested_notional",
        "released_cash",
        "price",
        "anchor_price",
        "reserve",
        "unallocated_reserve",
        "locked_reserve",
        "total_wealth",
        "current_cash_available",
        "future_contributions_before_activation",
        "immediate_notional",
        "deferred_notional",
    ):
        return _amount(value, quote)
    return _display(value)


def _details(lines: list[str], title: str, values: dict, quote: str) -> None:
    lines.extend(["", f"### {title}", ""])
    for key, value in values.items():
        if isinstance(value, dict):
            _details(lines, f"{title}: {_label(key)}", value, quote)
        elif isinstance(value, list) and any(isinstance(item, dict) for item in value):
            for index, item in enumerate(value, 1):
                if isinstance(item, dict):
                    _details(lines, f"{title}: {_label(key)} {index}", item, quote)
                else:
                    lines.append(f"- {_label(key)} {index}: {_display(item)}")
        else:
            lines.append(f"- {_label(key)}: {_value(key, value, quote)}")


def _frequency(value):
    if isinstance(value, dict):
        return {_label(key): _frequency(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_frequency(item) for item in value]
    return _percent(value)


def _context(lines, result, quote):
    rows = []
    for horizon, metrics in result.get("lookbacks", {}).items():
        for name, metric in metrics.items():
            if not isinstance(metric, dict):
                continue
            coverage = metric.get("coverage") or {}
            value = (
                _percent(metric.get("value"))
                if metric.get("unit") in ("fraction", "annualized_fraction")
                or name in ("return", "decline_from_high", "range_position")
                else _display(metric.get("value"))
            )
            rows.append(
                [
                    horizon,
                    _label(name),
                    value,
                    metric.get("status", "unknown"),
                    coverage.get("required_days"),
                    coverage.get("obtained_days"),
                    coverage.get("missing_days"),
                    metric.get("reason"),
                ]
            )
    _table(
        lines,
        [
            "Lookback days",
            "Metric",
            "Value",
            "Status",
            "Required days",
            "Obtained days",
            "Missing days",
            "Reason",
        ],
        rows,
    )
    for period, metrics in result.get("calendar", {}).items():
        _details(lines, f"Calendar {period}", metrics, quote)
    lines.append(
        "Unknown values retain their coverage limits. A historical rank does not establish undervaluation."
    )


def _windows(lines, result, quote):
    anchor = result.get("anchor") or {}
    lines.extend(
        [
            "",
            f"Observed anchor: {_amount(result.get('anchor_price', anchor.get('price')), quote)} at {_date(result.get('anchor_time', anchor.get('time')))}.",
        ]
    )
    rows = []
    for horizon, levels in result.get("windows", {}).items():
        for threshold, row in levels.items():
            rows.append(
                [
                    horizon,
                    _percent(threshold),
                    row.get("status"),
                    row.get("effective_samples"),
                    _frequency(row.get("conditional_frequencies")),
                    _frequency(row.get("reference_frequencies")),
                    _frequency(row.get("uncertainty")),
                    row.get("insufficient_reason") or "none",
                ]
            )
    _table(
        lines,
        [
            "Days",
            "Decline threshold",
            "Status",
            "Effective samples",
            "Conditional frequencies",
            "Reference frequencies",
            "Uncertainty",
            "Reason",
        ],
        rows,
    )
    lines.append(
        "Historical frequencies are experimental. A touched threshold does not establish an executable fill; no future price path is implied."
    )


def _paired_wealth(terminal, baseline) -> str:
    if terminal.get("status") != "complete" or baseline.get("status") != "complete":
        return "unknown"
    if any(
        terminal.get(key) is None or terminal.get(key) != baseline.get(key)
        for key in ("time", "price_time", "price", "contributions")
    ):
        return "unknown: incomparable_cashflows_or_valuation"
    if terminal.get("total_wealth") is None or baseline.get("total_wealth") is None:
        return "unknown"
    with localcontext() as context:
        context.prec = max(28, len(str(terminal["total_wealth"])) + len(str(baseline["total_wealth"])) + 2)
        return format(Decimal(terminal["total_wealth"]) - Decimal(baseline["total_wealth"]), "f")


def _plans(lines, result, quote):
    plans = result.get("plans", {})
    _table(
        lines,
        [
            "Plan",
            "Quantity acquired",
            "New purchase all-in cost",
            "Reserve",
            "Free cash",
            "Locked cash",
            "Maximum drawdown",
        ],
        [
            [
                name,
                row.get("net_quantity"),
                _amount(row.get("new_purchases_all_in_cost"), quote),
                _amount(row.get("reserve"), quote),
                _amount(row.get("unallocated_reserve"), quote),
                _amount(row.get("locked_reserve"), quote),
                _percent(row.get("maximum_drawdown")),
            ]
            for name, row in plans.items()
        ],
    )
    terminals = []
    baseline = plans.get("regular", {}).get("terminals", {})
    for name, row in plans.items():
        for horizon, terminal in row.get("terminals", {}).items():
            delta = _paired_wealth(terminal, baseline.get(horizon, {}))
            terminals.append(
                [
                    name,
                    horizon,
                    terminal.get("status"),
                    _date(terminal.get("time")),
                    _amount(terminal.get("total_wealth"), quote),
                    _amount(delta, quote) if not delta.startswith("unknown") else delta,
                    terminal.get("reason") or "none",
                    terminal.get("maximum_waiting_seconds"),
                    terminal.get("waiting_seconds_total"),
                    terminal.get("cash_waiting_quote_days"),
                ]
            )
    _table(
        lines,
        [
            "Plan",
            "Days",
            "Status",
            "Terminal time",
            "Holdings plus reserve",
            "Wealth difference vs regular",
            "Reason",
            "Maximum waiting seconds",
            "Waiting seconds total",
            "Cash waiting quote days",
        ],
        terminals,
    )
    for name, row in plans.items():
        lines.extend(["", f"### {name}: waiting and execution", ""])
        for field in ("waiting_targets", "expired_targets", "funding", "unfilled_purchases", "touches"):
            entries = row.get(field, [])
            lines.append(f"{_label(field)}: {len(entries)}")
            if entries:
                keys = list(dict.fromkeys(key for entry in entries for key in entry))
                table_rows = []
                for entry in entries:
                    values = []
                    for key in keys:
                        value = entry.get(key)
                        if key in (
                            "time",
                            "created",
                            "deadline",
                            "execution_deadline",
                            "interval_start",
                            "interval_end",
                        ):
                            value = _date(value)
                        elif key in ("target", "requested_notional", "allocated_cash", "released_cash"):
                            value = _amount(value, quote)
                        values.append(value)
                    table_rows.append(values)
                _table(lines, [_label(key) for key in keys], table_rows)
    lines.append(
        "Lower acquisition cost alone does not establish better total wealth. Historical execution is hypothetical."
    )


def _evaluation(lines, result, quote):
    lines.extend(
        [
            "",
            f"Sample size: {_display(result.get('sample_size'))}",
            f"Excluded origins: {len(result.get('excluded_origins', []))}",
        ]
    )
    for key in ("scenario_sample_size", "unqualified_scenarios"):
        if key in result:
            lines.append(f"{_label(key)}: {_display(result[key])}")
    rows, dispersion, other_metrics = [], [], []
    quote_metrics = {
        "acquisition_cost_difference",
        "terminal_wealth_difference",
        "uninvested_reserve",
        "reserve_difference",
    }
    for horizon, families in result.get("metrics", {}).items():
        for name, metrics in families.items():
            rows.append(
                [
                    horizon,
                    name,
                    metrics.get("sample_size"),
                    metrics.get("underperformance_count"),
                    _percent(metrics.get("underperformance_fraction")),
                    metrics.get("missed_rising_market_exposure_count"),
                    metrics.get("maximum_waiting_seconds"),
                ]
            )
            for metric, values in metrics.items():
                if not isinstance(values, dict):
                    if metric not in {
                        "sample_size",
                        "underperformance_count",
                        "underperformance_fraction",
                        "missed_rising_market_exposure_count",
                        "maximum_waiting_seconds",
                    }:
                        other_metrics.append([horizon, name, _label(metric), _display(values)])
                    continue

                def display(value, metric=metric):
                    if metric in quote_metrics:
                        return _amount(value, quote)
                    return _percent(value) if metric == "maximum_drawdown" else _display(value)

                dispersion.append(
                    [
                        horizon,
                        name,
                        _label(metric),
                        *[
                            display(values.get(key))
                            for key in ("mean", "minimum", "maximum", "population_standard_deviation")
                        ],
                    ]
                )
    _table(
        lines,
        [
            "Days",
            "Plan",
            "Sample size",
            "Underperformance count",
            "Underperformance fraction",
            "Missed rising market exposure count",
            "Maximum waiting seconds",
        ],
        rows,
    )
    _table(lines, ["Days", "Plan", "Metric", "Value"], other_metrics)
    _table(
        lines,
        ["Days", "Plan", "Metric", "Mean", "Minimum", "Maximum", "Population standard deviation"],
        dispersion,
    )
    origins = []
    for record in result.get("origins", []):
        for horizon, outcome in record.get("outcomes", {}).items():
            if outcome.get("status") != "complete":
                origins.append(
                    [
                        _date(record.get("origin")),
                        horizon,
                        "all",
                        outcome.get("status"),
                        "unknown",
                        "unknown",
                        "unknown",
                        "unknown",
                        "unknown",
                    ]
                )
                continue
            for name in ("split", "deferred"):
                row = outcome.get(name, {})
                origins.append(
                    [
                        _date(record.get("origin")),
                        horizon,
                        name,
                        outcome.get("status"),
                        _amount(row.get("terminal_wealth_difference"), quote),
                        _amount(row.get("uninvested_reserve"), quote),
                        row.get("waiting_seconds_total"),
                        row.get("expired_targets"),
                        outcome.get("evidence_qualification", "unknown"),
                    ]
                )
    _table(
        lines,
        [
            "Origin",
            "Days",
            "Plan",
            "Status",
            "Wealth difference vs regular",
            "Uninvested reserve",
            "Waiting seconds total",
            "Expired targets",
            "Evidence qualification",
        ],
        origins,
    )
    _table(
        lines,
        ["Excluded origin", "Reason"],
        [[_date(row.get("origin")), row.get("reason")] for row in result.get("excluded_origins", [])],
    )
    _details(lines, "Evaluation protocol", result.get("protocol", {}), quote)
    lines.append(
        "Completed terminal valuations do not by themselves establish complete execution-path evidence or prospective performance."
    )


def _brief(lines, result, quote):
    observed = result.get("current_observed", {})
    anchor = observed.get("anchor", {})
    lines.extend(
        [
            "",
            "## Current observed conditions",
            "",
            f"Observed anchor: {_amount(anchor.get('price'), quote)} at {_date(anchor.get('time'))}; status {_display(anchor.get('status'))}; age {_display(anchor.get('age_seconds'))} seconds.",
        ]
    )
    context_rows = []
    for horizon, metrics in observed.get("context", {}).get("lookbacks", {}).items():
        for name in ("return", "decline_from_high", "range_position"):
            metric = metrics.get(name, {})
            coverage = metric.get("coverage", {})
            context_rows.append(
                [
                    horizon,
                    _label(name),
                    _percent(metric.get("value")),
                    metric.get("status"),
                    coverage.get("required_days"),
                    coverage.get("obtained_days"),
                    metric.get("reason") or "none",
                ]
            )
    _table(
        lines,
        ["Lookback days", "Context metric", "Value", "Status", "Required days", "Obtained days", "Reason"],
        context_rows,
    )
    if isinstance(observed.get("purchase_windows"), dict):
        lines.extend(["", "## Purchase window support"])
        _windows(lines, observed["purchase_windows"], quote)
    policy = result.get("hypothetical_policy", {})
    lines.extend(["", "## Hypothetical declared policy", "", f"Status: {_display(policy.get('status'))}."])
    config = policy.get("config", {})
    if config:
        _details(lines, "Declared fees", config.get("fees", {}), quote)
        _details(lines, "Declared execution costs", config.get("execution", {}), quote)
        lines.append(
            f"Split and deferred policies equivalent: {_display(policy.get('funding_preview', {}).get('policies_equivalent'))}."
        )
    next_tranche = policy.get("next_tranche")
    if next_tranche:
        lines.extend(
            [
                "",
                f"Next tranche: {_amount(next_tranche.get('requested_notional'), quote)} at {_date(next_tranche.get('time'))}.",
            ]
        )
        families, allocations = [], []
        for family, row in next_tranche.get("families", {}).items():
            families.append(
                [
                    family,
                    *[
                        _amount(row.get(key), quote)
                        for key in (
                            "current_cash_available",
                            "future_contributions_before_activation",
                            "immediate_notional",
                            "deferred_notional",
                            "free_cash_after",
                            "locked_cash_after",
                        )
                    ],
                    row.get("funding_shortfall"),
                ]
            )
            for allocation in row.get("allocations", []):
                allocations.append(
                    [
                        family,
                        _amount(allocation.get("requested_notional"), quote),
                        _percent(allocation.get("decline")),
                        _amount(allocation.get("target_price"), quote),
                        allocation.get("target_interpretation"),
                        _date(allocation.get("deadline")),
                        allocation.get("fallback"),
                        _amount(allocation.get("required_cash"), quote),
                        _amount(allocation.get("allocated_cash"), quote),
                        allocation.get("funding_shortfall"),
                    ]
                )
        _table(
            lines,
            [
                "Policy",
                "Current free cash",
                "Future deposits before activation",
                "Immediate notional",
                "Deferred notional",
                "Free cash after",
                "Locked cash after",
                "Funding shortfall",
            ],
            families,
        )
        _table(
            lines,
            [
                "Policy",
                "Requested notional",
                "Decline",
                "Target price",
                "Target interpretation",
                "Deadline",
                "Fallback",
                "Required cash",
                "Allocated cash",
                "Funding shortfall",
            ],
            allocations,
        )
        lines.append(_display(next_tranche.get("funding_basis")))
    else:
        lines.extend(["", "Next tranche: unavailable (no upcoming declared tranche)."])
    current = policy.get("current_simulation")
    if current:
        _table(
            lines,
            [
                "Hypothetical policy state",
                "Free cash",
                "Locked cash",
                "Waiting targets",
                "Unfilled purchases",
            ],
            [
                [
                    name,
                    _amount(row.get("unallocated_reserve"), quote),
                    _amount(row.get("locked_reserve"), quote),
                    row.get("pending_targets"),
                    len(row.get("unfilled_purchases", [])),
                ]
                for name, row in current.get("plans", {}).items()
            ],
        )
    pending = []
    for family, targets in policy.get("pending_targets", {}).items():
        for target in targets:
            pending.append(
                [
                    family,
                    _amount(target.get("allocated_cash"), quote),
                    _amount(target.get("target"), quote),
                    _date(target.get("deadline")),
                    target.get("fallback"),
                    target.get("waiting_seconds"),
                    target.get("interpretation"),
                ]
            )
    if pending:
        _table(
            lines,
            [
                "Pending policy target",
                "Allocated cash",
                "Threshold price",
                "Deadline",
                "Fallback",
                "Waiting seconds",
                "Interpretation",
            ],
            pending,
        )
    outcomes = []
    for horizon, row in result.get("historical_outcomes", {}).items():
        comparisons = row.get("comparisons", {}) if row.get("status") == "complete" else {}
        outcomes.append(
            [
                horizon,
                _date(row.get("time")),
                row.get("status"),
                row.get("reason") or "none",
                _amount(comparisons.get("split", {}).get("terminal_wealth_difference"), quote),
                _amount(comparisons.get("deferred", {}).get("terminal_wealth_difference"), quote),
            ]
        )
    lines.extend(["", "## Historical outcomes"])
    _table(
        lines,
        [
            "Days",
            "Terminal time",
            "Status",
            "Reason",
            "Split wealth difference vs regular",
            "Deferred wealth difference vs regular",
        ],
        outcomes,
    )


def _sensitivity(lines, result, quote):
    lines.extend(
        [
            "",
            f"Baseline cost case: {_display(result.get('baseline_case'))}.",
            "Differences within a case are policy minus regular; differences from baseline are current cost case minus first cost case.",
        ]
    )
    for case in result.get("cases", []):
        name = _display(case.get("name"))
        lines.extend(["", f"## Cost case: {name}"])
        _details(lines, f"{name} cost assumptions", case.get("costs", {}), quote)
        _plans(lines, case, quote)
        for horizon, outcome in case.get("outcomes", {}).items():
            lines.extend(
                [
                    "",
                    f"### {name}: {horizon}-day comparison",
                    "",
                    f"Status: {_display(outcome.get('status'))}.",
                ]
            )
            _details(lines, "Evidence qualification", outcome.get("evidence_qualification", {}), quote)
            if outcome.get("status") == "complete":
                _details(lines, "Paired differences vs regular", outcome.get("paired_differences", {}), quote)
        _details(
            lines,
            f"{name} differences from baseline cost case",
            case.get("differences_from_baseline", {}),
            quote,
        )
    reversals = result.get("ranking_reversals", [])
    lines.extend(["", f"Ranking reversals: {len(reversals)}."])
    for reversal in reversals:
        _details(lines, "Ranking reversal", reversal, quote)
    lines.append("Cost cases are caller assumptions. No cost case or policy is selected as preferred.")


def _nested(lines, result, quote):
    for key, kind in (("context", "context"), ("windows", "windows"), ("plan", "plans")):
        nested = result.get(key)
        if isinstance(nested, dict):
            report = render_accumulation_report(nested, kind)
            lines.extend(["", report.replace("# ", "## ", 1).rstrip()])
        elif key == "windows" and isinstance(nested, list):
            rows = []
            for window in nested:
                endpoint = window.get("endpoint_below_anchor") or {}
                observable = window.get("observable_endpoint") or {}
                touch = window.get("below_anchor_touch") or {}
                rows.append(
                    [
                        window.get("horizon_days"),
                        _percent(window.get("threshold")),
                        window.get("status"),
                        touch.get("state"),
                        endpoint.get("state"),
                        endpoint.get("interval"),
                        observable.get("state"),
                        _date(observable.get("mark_time")),
                        observable.get("offset_seconds"),
                        window.get("coverage"),
                    ]
                )
            _table(
                lines,
                [
                    "Days",
                    "Decline threshold",
                    "Status",
                    "Threshold touch",
                    "Endpoint below anchor",
                    "Endpoint reason",
                    "Separate observable hourly mark",
                    "Hourly mark time",
                    "Offset seconds",
                    "Coverage",
                ],
                rows,
            )
        elif nested is not None:
            lines.append(f"{_label(key)}: {_display(nested)}")


def render_accumulation_report(result: dict, kind: str) -> str:
    """Render public calculation or journal envelopes without changing exact JSON."""
    envelope = result
    result = result.get("payload") if isinstance(result.get("payload"), dict) else result
    market = result.get("market") or result.get("input_reference") or result.get("config") or result
    mode = market.get("data_mode") or result.get("data_mode") or "unknown"
    quote = market.get("quote") or result.get("quote") or "unknown"
    lines = [
        f"# {kind.replace('-', ' ').title()}",
        "",
        f"Market: {market.get('asset') or result.get('asset') or 'unknown'}/{quote} · Source: {market.get('source') or result.get('source') or 'unknown'}.",
        f"As of: {_date(result.get('as_of', market.get('as_of')))} · Mode: {mode} · Status: {result.get('status', 'computed')}.",
    ]
    if mode in ("demo", "synthetic"):
        lines.append("SYNTHETIC DEMONSTRATION: these observations are not live market evidence.")
    for key in ("assessment_id", "issued_at", "observed_at", "content_hash"):
        if key in envelope:
            value = _date(envelope[key]) if key.endswith("_at") else _display(envelope[key])
            lines.append(f"{_label(key)}: {value}.")
    for key in (
        "input_reference",
        "input_fingerprint",
        "expires_at",
        "validation_mode",
        "coverage",
        "input_truncation",
        "replay",
        "reason",
        "historical_calculation",
        "historical_availability_verified",
    ):
        if key in result:
            value = _date(result[key]) if key == "expires_at" else _display(result[key])
            lines.append(f"{_label(key)}: {value}.")
    if result is not envelope or kind in ("issue", "assessment", "observe"):
        _nested(lines, result, quote)
    elif kind == "accumulation-brief" or result.get("kind") == "accumulation_brief":
        _brief(lines, result, quote)
    elif kind == "cost-sensitivity":
        _sensitivity(lines, result, quote)
    elif kind == "context":
        _context(lines, result, quote)
    elif kind == "windows":
        _windows(lines, result, quote)
    elif kind in ("compare-plans", "plans"):
        _plans(lines, result, quote)
    elif kind == "evaluate-plans":
        _evaluation(lines, result, quote)
    elif kind == "plan-preview":
        _details(lines, "Declared fees", result.get("fees", {}), quote)
        lines.extend(["", f"Split and deferred policies equivalent: {_display(result.get('policies_equivalent'))}."])
        for family, preview in result.get("families", {}).items():
            _details(lines, f"{family} current funding constraints", preview.get("current", {}), quote)
            _details(lines, f"{family} next tranche", preview.get("next_tranche") or {}, quote)
        _details(lines, "Future contributions", {str(index): row for index, row in enumerate(result.get("future_contributions", []))}, quote)
    elif kind == "journal-summary":
        _details(lines, "Selection and bounds", result.get("selection", {}), quote)
        _details(lines, "Issue/event counts (not independent samples)", result.get("counts", {}), quote)
        rows = []
        for cohort in result.get("cohorts", []):
            identity, counts = cohort["identity"], cohort["counts"]
            rows.append([identity.get("asset"), identity.get("quote"), identity.get("source"),
                         identity.get("data_mode"), identity.get("event"), identity.get("horizon_days"),
                         identity.get("threshold"), counts.get("issued"), counts.get("resolved"),
                         counts.get("pending"), counts.get("missing"), counts.get("ambiguous"),
                         counts.get("excluded"), cohort.get("overlapping_pairs")])
        _table(lines, ["Asset", "Quote", "Source", "Mode", "Event", "Days", "Threshold", "Issued",
                       "Resolved", "Pending", "Missing", "Ambiguous", "Excluded", "Overlapping pairs"], rows)
        _details(lines, "Plan pending horizons (unscored)", result.get("plan_pending", {}), quote)
    else:
        for key in ("state", "reasons", "supporting_evidence", "missing_sources", "summary", "comparisons"):
            if key in result:
                lines.append(f"{_label(key)}: {_display(result[key])}.")
    for key in ("contradictions", "contradictory_evidence", "limitations", "assumptions"):
        if result.get(key):
            lines.extend(["", f"{_label(key)}:", ""])
            entries = result[key] if isinstance(result[key], list) else [result[key]]
            lines.extend(f"- {_display(entry)}" for entry in entries)
    return "\n".join(lines) + "\n"
