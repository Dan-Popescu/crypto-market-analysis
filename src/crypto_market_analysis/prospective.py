"""Read-only, bounded journal denominators; these are not performance scores."""

from __future__ import annotations

from datetime import datetime

from .evidence import canonical_json, restore_evidence

DAY = 86400
_COUNTS = ("issued", "mature", "resolved", "ambiguous", "missing", "pending", "excluded")
_MARKET = ("source", "asset", "quote", "data_mode")
_OBSERVABLE = "hourly_after_deadline_v1"


def _clock(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return int(parsed.timestamp())
        except ValueError:
            pass
    return None


def _counts():
    return dict.fromkeys(_COUNTS, 0)


def _chronology(record):
    payload = record["payload"]
    reference = payload.get("input_reference", {})
    issued = record["issued_at"]
    cutoff = _clock(reference.get("as_of"))
    available = _clock(reference.get("available_at"))
    if available is None and record.get("evidence"):
        evidence = restore_evidence(record["evidence"])
        clocks = [_clock(row.get("observed_at")) for row in evidence.get("candles", [])]
        if clocks and all(clock is not None for clock in clocks):
            available = max(clocks)
    exclusions = []
    if cutoff is None or cutoff > issued:
        exclusions.append("invalid_evidence_cutoff")
    elif cutoff < issued or payload.get("historical_calculation"):
        exclusions.append("historical_evidence_cutoff")
    if available is None:
        exclusions.append("availability_unverified")
    elif available > issued or (cutoff is not None and available > cutoff):
        exclusions.append("training_not_available_by_cutoff")
    if reference.get("data_mode") not in ("live", "import"):
        exclusions.append("non_market_data_mode")
    if any(not reference.get(key) for key in _MARKET):
        exclusions.append("market_identity_incomplete")
    return {"issued_at": issued, "evidence_cutoff": cutoff, "training_available_at": available,
            "temporality": "retrospective" if "historical_evidence_cutoff" in exclusions
            else "unverified" if exclusions else "prospective", "exclusions": exclusions}


def _eligible_outcomes(record, outcomes, as_of):
    reference = record["payload"].get("input_reference", {})
    eligible = []
    rejected = []
    for outcome in outcomes:
        observed = _clock(outcome.get("observed_at"))
        payload = outcome.get("payload", {})
        cutoff = _clock(payload.get("as_of"))
        if observed is None or observed > as_of:
            continue
        other = payload.get("input_reference", {})
        if observed < record["issued_at"] or cutoff is None or not record["issued_at"] <= cutoff <= observed:
            rejected.append({"outcome_hash": outcome.get("content_hash"), "reason": "invalid_outcome_chronology"})
        elif any(other.get(key) != reference.get(key) for key in (*_MARKET, "dataset_lineage")):
            rejected.append({"outcome_hash": outcome.get("content_hash"), "reason": "outcome_lineage_mismatch"})
        else:
            eligible.append(outcome)
    return sorted(eligible, key=lambda row: (row["observed_at"], row.get("content_hash", ""))), rejected


def _event_result(outcomes, event, horizon, threshold, deadline, as_of):
    candidates = []
    for outcome in outcomes:
        for window in outcome["payload"].get("windows", []):
            cutoff = _clock(window.get("as_of"))
            if (window.get("horizon_days") != horizon
                    or (threshold is not None and window.get("threshold") != threshold)
                    or cutoff is None or cutoff > outcome["observed_at"] or cutoff > as_of):
                continue
            value = window.get(event)
            if not isinstance(value, dict):
                continue
            # A touch can be established early. Endpoint evidence requires maturity.
            if event != "below_anchor_touch" and cutoff < deadline:
                continue
            if value.get("state") == "known_false" and cutoff < deadline:
                continue
            candidates.append((outcome, value))
    definite = next(((outcome, value) for outcome, value in candidates
                     if value.get("state") in ("known_true", "known_false")), None)
    selected = definite or (candidates[-1] if candidates else None)
    if selected:
        outcome, value = selected
        state = value.get("state")
        status = "resolved" if definite else "pending" if as_of < deadline else (
            "ambiguous" if state == "ambiguous" else "missing")
        return {"status": status, "state": state,
                "value": state == "known_true" if definite else None,
                "outcome_hash": outcome.get("content_hash"), "observed_at": outcome["observed_at"]}
    return {"status": "pending" if as_of < deadline else "missing", "state": None,
            "value": None, "outcome_hash": None, "observed_at": None}


def _plan_summary(record, chronology, outcomes, as_of):
    plan = record["payload"].get("plan")
    if not isinstance(plan, dict):
        return None
    start = _clock(plan.get("config", {}).get("start_time"))
    issued = record["issued_at"]
    temporality = chronology["temporality"]
    if start is None:
        temporality = "unverified"
    elif start < issued:
        temporality = "retrospective" if start + 180 * DAY <= issued else "hybrid"
    terminals = {}
    for days in (90, 180):
        deadline = start + days * DAY if start is not None else None
        selected = None
        for outcome in outcomes:
            result = outcome["payload"].get("plan", {})
            scenarios = list(result.get("plans", {}).values())
            cutoff = _clock(result.get("as_of"))
            if (deadline is not None and cutoff is not None
                    and deadline <= cutoff <= outcome["observed_at"]
                    and scenarios and all(row.get("terminals", {}).get(str(days), {}).get("status")
                                          == "complete" for row in scenarios)):
                selected = outcome
                break
        terminals[str(days)] = {"deadline": deadline,
                                "status": "observed_unscored" if selected else "pending"
                                if deadline is not None and as_of < deadline else "missing",
                                "outcome_hash": selected.get("content_hash") if selected else None}
    return {"assessment_id": record["assessment_id"], "start_time": start, "temporality": temporality,
            "rule_version": plan.get("rule_version"), "terminals": terminals,
            "qualified_performance_samples": 0,
            "unscored_reason": "Plan performance aggregation is not implemented; terminal prices alone do not qualify execution paths."}


def summarize_journal(records: list[dict], outcomes_by_id: dict[str, list[dict]], as_of: int) -> dict:
    """Count each frozen issue/event once, retaining missing and excluded denominators.

    Input and output bounds are 100 originals and 256 appended outcomes per original.
    Observable endpoints require a declaration in the original; legacy exact events
    remain distinct. Revisions cannot replace the first definite observed outcome.
    """
    if _clock(as_of) != as_of or type(as_of) is not int:
        raise ValueError("as_of must be a nonnegative integer UTC epoch second")
    if not isinstance(records, list) or not isinstance(outcomes_by_id, dict):
        raise ValueError("Expected journal records and outcomes grouped by assessment ID")
    cohorts, plans, seen = {}, [], set()
    duplicates = ignored = 0
    truncated_outcomes = {}
    for record in records[:100]:
        identity = record["assessment_id"]
        if identity in seen:
            duplicates += 1
            continue
        if record["issued_at"] > as_of or record.get("payload", {}).get("kind") != "accumulation_assessment":
            ignored += 1
            continue
        seen.add(identity)
        chronology = _chronology(record)
        appended = outcomes_by_id.get(identity, [])
        if len(appended) > 256:
            truncated_outcomes[identity] = len(appended) - 256
        outcomes, rejected = _eligible_outcomes(record, appended[:256], as_of)
        payload = record["payload"]
        reference = payload.get("input_reference", {})
        protocol = payload.get("outcome_protocol", {})
        for horizon_key, thresholds in payload.get("windows", {}).get("windows", {}).items():
            horizon = int(horizon_key)
            events = [("endpoint_below_anchor", None, "exact_deadline_v1")]
            events.extend(("below_anchor_touch", float(threshold), "fully_contained_hourly_bars_v1")
                          for threshold in thresholds)
            if protocol.get("endpoint_convention") == _OBSERVABLE:
                events.append(("observable_endpoint", None, _OBSERVABLE))
            for event, threshold, convention in events:
                deadline = record["issued_at"] + horizon * DAY
                if event == "observable_endpoint":
                    deadline = (deadline + 3599) // 3600 * 3600
                cohort_identity = {**{key: reference.get(key) for key in _MARKET},
                                   "dataset_lineage": reference.get("dataset_lineage"),
                                   "rules": payload.get("rules", {}), "event": event,
                                   "event_convention": convention, "horizon_days": horizon, "threshold": threshold}
                cohort = cohorts.setdefault(canonical_json(cohort_identity), {
                    "identity": cohort_identity, "counts": _counts(), "events": []})
                result = _event_result(outcomes, event, horizon, threshold, deadline, as_of)
                row = {"assessment_id": identity, "assessment_hash": record.get("content_hash"),
                       **chronology, "deadline": deadline, **result, "rejected_outcomes": rejected}
                if chronology["exclusions"]:
                    row["observed_status"] = row["status"]
                    row["status"] = "excluded"
                cohort["events"].append(row)
                cohort["counts"]["issued"] += 1
                cohort["counts"]["mature"] += as_of >= deadline
                cohort["counts"][row["status"]] += 1
        plan = _plan_summary(record, chronology, outcomes, as_of)
        if plan is not None:
            plans.append(plan)
    totals = _counts()
    for cohort in cohorts.values():
        for key, count in cohort["counts"].items():
            totals[key] += count
        events = cohort["events"]
        cohort["overlapping_pairs"] = sum(
            max(left["issued_at"], right["issued_at"]) < min(left["deadline"], right["deadline"])
            for index, left in enumerate(events) for right in events[index + 1:])
    return {"schema_version": 1, "as_of": as_of, "assessment_count": len(seen),
            "duplicate_assessments": duplicates, "ignored_assessments": ignored,
            "counts": totals, "cohorts": [cohorts[key] for key in sorted(cohorts)], "plans": plans,
            "plan_pending": {str(days): sum(plan["terminals"][str(days)]["status"] == "pending"
                                           for plan in plans) for days in (90, 180)},
            "truncation": {"assessments_omitted": max(0, len(records) - 100),
                           "outcomes_omitted": truncated_outcomes},
            "limitations": ["Counts describe issue/events, not independent samples; overlapping windows remain correlated.",
                            "First definite eligible append is retained; later revisions are not extra samples.",
                            "Historical anchor-relative estimates are not scored against issuance-relative outcomes.",
                            "No calibrated probability, predictive edge or accumulation advantage is established."]}
