"""Deterministic accumulation reports and bounded structured input validation."""

from __future__ import annotations

import json
import math


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


def render_accumulation_report(result: dict, kind: str) -> str:
    """Explain shared-engine evidence without adding a score or recommendation."""
    market = result.get("market") or result.get("config") or result
    mode = market.get("data_mode", result.get("data_mode", "unknown"))
    lines = [
        f"# {kind.replace('-', ' ').title()}",
        "",
        f"Market: {market.get('asset') or 'unknown'}/{market.get('quote') or 'unknown'} · Source: {market.get('source') or 'unknown'}.",
        f"As of: {_display(result.get('as_of'))} · Mode: {mode} · Status: {result.get('status', 'computed')}.",
    ]
    if mode in ("demo", "synthetic"):
        lines.append("SYNTHETIC DEMONSTRATION: these observations are not live market evidence.")
    for key in ("input_reference", "input_fingerprint", "expires_at", "validation_mode", "coverage"):
        if key in result:
            lines.append(f"{key.replace('_', ' ').capitalize()}: {_display(result[key])}.")
    if kind == "context":
        lines.extend(
            [
                "",
                "| Lookback days | Return | Decline from observed high | Range position |",
                "| --- | --- | --- | --- |",
            ]
        )
        for name, row in result.get("lookbacks", {}).items():
            cells = [
                _display((row.get(metric) or {}).get("value"))
                for metric in ("return", "decline_from_high", "range_position")
            ]
            lines.append(f"| {name} | {' | '.join(cells)} |")
        lines.append(
            "Values are fractions; unknown values retain their coverage limits. A historical rank does not establish undervaluation."
        )
    elif kind == "windows":
        lines.extend(
            [
                "",
                f"Observed anchor: {_display(result.get('anchor_price'))} at {_display(result.get('anchor_time'))}.",
                "| Days | Decline threshold | Status | Effective samples | Conditional frequencies | Reference frequencies | Uncertainty |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for horizon, levels in result.get("windows", {}).items():
            for threshold, row in levels.items():
                cells = [
                    horizon,
                    threshold,
                    row.get("status"),
                    row.get("effective_samples"),
                    row.get("conditional_frequencies"),
                    row.get("reference_frequencies"),
                    row.get("uncertainty"),
                ]
                lines.append("| " + " | ".join(_display(cell) for cell in cells) + " |")
        lines.append(
            "Historical frequencies are experimental. A touched threshold does not establish an executable fill; no future price path is implied."
        )
    elif kind in ("compare-plans", "plans"):
        lines.extend(
            [
                "",
                "| Plan | Quantity acquired | New purchase all-in cost | Reserve | Maximum drawdown |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for name, row in result.get("plans", {}).items():
            cells = [
                name,
                row.get("net_quantity"),
                row.get("new_purchases_all_in_cost"),
                row.get("reserve"),
                row.get("maximum_drawdown"),
            ]
            lines.append("| " + " | ".join(_display(cell) for cell in cells) + " |")
            for horizon, terminal in row.get("terminals", {}).items():
                lines.append(
                    f"{name}, {horizon} days: {terminal.get('status', 'unknown')}; holdings plus reserve {_display(terminal.get('total_wealth'))}."
                )
        lines.append(
            "Lower acquisition cost alone does not establish better total wealth. Historical execution is hypothetical."
        )
    else:
        for key in (
            "state",
            "reasons",
            "supporting_evidence",
            "missing_sources",
            "summary",
            "comparisons",
            "assessment_id",
        ):
            if key in result:
                lines.append(f"{key.replace('_', ' ').capitalize()}: {_display(result[key])}.")
    for key in ("contradictions", "contradictory_evidence", "limitations", "assumptions"):
        if result.get(key):
            lines.extend(["", f"{key.replace('_', ' ').capitalize()}:"])
            entries = result[key] if isinstance(result[key], list) else [result[key]]
            lines.extend(f"- {_display(entry)}" for entry in entries)
    return "\n".join(lines) + "\n"
