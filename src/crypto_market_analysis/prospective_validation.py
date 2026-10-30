"""Frozen-protocol, known-at validation with bounded, read-only cohort pages.

Calibration is descriptive. Existing anchor-relative frequencies only qualify
when their frozen origin, deadline and event exactly match the evaluated event.
"""

from __future__ import annotations

import math
from copy import deepcopy

from .journal import AssessmentJournal
from .prospective import DAY, _clock, assessment_chronology, select_outcomes
from .scorecards import build_scorecard
from .validation_protocol import (
    EVENT_FIELDS,
    MARKET_FIELDS,
    fraction,
    utc_second,
    validate_validation_protocol,
)

MODEL_VERSION = "purchase_windows_v1"
EVENT_RULE_VERSION = "issuance_hourly_v1"
MAX_RECORDS = 1000
MAX_OUTCOMES = 256


def _matches(record, cohort):
    reference = record.get("payload", {}).get("input_reference", {})
    return all(reference.get(key) == cohort[key] for key in (*MARKET_FIELDS, "dataset_lineage"))


def build_validation_predictions(protocol, reference, windows, rules, issued_at):
    """Derive comparable probabilities from the actual frozen calculation only.

    This helper never copies user supplied probabilities. An issuance between
    hourly anchors emits no prediction. Observable endpoints have a distinct
    convention and are deliberately not inferred from historical exact endpoints.
    """
    protocol = validate_validation_protocol(protocol)
    utc_second(issued_at, "issued_at")
    if (protocol["frozen_at"] > issued_at or _clock(windows.get("issued_at")) != issued_at
            or _clock(windows.get("anchor_time")) != issued_at or windows.get("stale") is not False
            or windows.get("schema_version") != 1
            or type(windows.get("anchor_price")) not in (int, float)
            or not math.isfinite(windows["anchor_price"]) or windows["anchor_price"] <= 0):
        return []
    predictions = []
    for cohort in protocol["declaration"]["cohorts"]:
        if (not all(reference.get(key) == cohort[key] for key in (*MARKET_FIELDS, "dataset_lineage"))
                or rules != cohort["rules"] or cohort["model_version"] != MODEL_VERSION
                or cohort["rule_version"] != EVENT_RULE_VERSION):
            continue
        for event in cohort["events"]:
            if event["event"] == "observable_endpoint":
                continue
            rows = windows.get("windows", {}).get(str(event["horizon_days"]), {})
            # Endpoint probabilities may be repeated for several touch thresholds.
            candidates = [row for row in rows.values()
                          if event["threshold"] is None or row.get("threshold") == event["threshold"]]
            candidate = next((row for row in candidates if row.get("issuance_alignment_matches") is True
                              and row.get("historical_alignment") == "anchor_relative_complete_24h_bars"
                              and _clock(row.get("estimate_anchor_time")) == issued_at
                              and _clock(row.get("estimate_window_end")) == issued_at + event["horizon_days"] * DAY
                              and _clock(row.get("purchase_window_end")) == issued_at + event["horizon_days"] * DAY
                              and row.get("status") == "experimental"), None)
            if candidate is None:
                continue
            probability = (candidate.get("conditional_frequencies") or {}).get(event["event"])
            try:
                probability = fraction(probability)
            except ValueError:
                continue
            predictions.append({**event, "protocol_hash": protocol["protocol_hash"],
                                "model_version": MODEL_VERSION, "rule_version": EVENT_RULE_VERSION,
                                "probability": probability, "window_start": issued_at,
                                "window_end": issued_at + event["horizon_days"] * DAY, "anchor_time": issued_at,
                                "anchor_price": windows["anchor_price"]})
    return predictions


def _reasons(protocol, record, cohort):
    reasons = assessment_chronology(record)["exclusions"]
    declaration = protocol["declaration"]
    if declaration["mode"] != "prospective":
        reasons.append("retrospective_protocol")
    if protocol["frozen_at"] > record["issued_at"]:
        reasons.append("protocol_frozen_after_issue")
    if record["payload"].get("validation_protocol_hash") != protocol["protocol_hash"]:
        reasons.append("protocol_not_bound_at_issue")
    if record["payload"].get("rules") != cohort["rules"]:
        reasons.append("frozen_rules_mismatch")
    return reasons


def _prediction(protocol, record, cohort, event):
    for prediction in record["payload"].get("validation_predictions") or []:
        if all(prediction.get(key) == event[key] for key in EVENT_FIELDS):
            if (prediction.get("protocol_hash") != protocol["protocol_hash"]
                    or prediction.get("model_version") != cohort["model_version"]
                    or prediction.get("rule_version") != cohort["rule_version"]):
                return None, "prediction_version_mismatch"
            issued = record["issued_at"]
            anchor = record["payload"].get("windows", {})
            if (prediction.get("window_start") != issued or prediction.get("anchor_time") != issued
                    or prediction.get("window_end") != issued + event["horizon_days"] * DAY
                    or _clock(anchor.get("anchor_time")) != issued
                    or type(prediction.get("anchor_price")) not in (int, float)
                    or not math.isfinite(prediction["anchor_price"]) or prediction["anchor_price"] <= 0
                    or prediction["anchor_price"] != anchor.get("anchor_price")):
                return None, "prediction_time_anchor_mismatch"
            try:
                return fraction(prediction.get("probability")), None
            except ValueError:
                return None, "invalid_probability"
    return None, "comparable_prediction_not_declared"


def _resolve(record, outcomes, event, deadline, as_of):
    candidates = []
    for outcome in outcomes:
        reference = outcome["payload"].get("input_reference", {})
        available = _clock(reference.get("available_at"))
        outcome_cutoff = _clock(outcome["payload"].get("as_of"))
        if available is None or outcome_cutoff is None or available > outcome_cutoff:
            continue
        for window in outcome["payload"].get("windows", []):
            cutoff = _clock(window.get("as_of"))
            if (window.get("horizon_days") != event["horizon_days"]
                    or (event["threshold"] is not None and window.get("threshold") != event["threshold"])
                    or _clock(window.get("issued_at")) != record["issued_at"]
                    or _clock(window.get("anchor_time")) != record["issued_at"]
                    or window.get("anchor_price") != record["payload"].get("windows", {}).get("anchor_price")
                    or _clock(window.get("window_end")) != record["issued_at"] + event["horizon_days"] * DAY
                    or cutoff is None or available > cutoff or cutoff > outcome_cutoff
                    or cutoff > outcome["observed_at"] or cutoff > as_of):
                continue
            value = window.get(event["event"], {})
            if event["event"] == "observable_endpoint" and (
                value.get("convention") != event["convention"]
            ):
                continue
            state = value.get("state")
            if cutoff < deadline and (event["event"] != "below_anchor_touch" or state != "known_true"):
                continue
            candidates.append((outcome, state))
    definite = next(((outcome, state) for outcome, state in candidates
                     if state in ("known_true", "known_false")), None)
    selected = definite or (candidates[-1] if candidates else None)
    outcome, state = selected if selected else ({}, None)
    status = "resolved" if definite else "pending" if as_of < deadline else (
        "ambiguous" if state == "ambiguous" else "missing")
    return {"status": status, "value": state == "known_true" if definite else None,
            "outcome_hash": outcome.get("content_hash"), "observed_at": outcome.get("observed_at")}


def _calibration(rows):
    samples = [row for row in rows if row["nonoverlapping_selection"] and row["status"] == "resolved"
               and row["mature"]]
    bins = [{"lower": index / 10, "upper": (index + 1) / 10, "sample_size": 0,
             "mean_probability": None, "observed_fraction": None} for index in range(10)]
    for index, bucket in enumerate(bins):
        selected = [row for row in samples if min(9, int(row["probability"] * 10)) == index]
        if selected:
            bucket.update(sample_size=len(selected),
                          mean_probability=sum(row["probability"] for row in selected) / len(selected),
                          observed_fraction=sum(row["value"] for row in selected) / len(selected))
    losses = [row["probability"] if row["value"] else 1 - row["probability"] for row in samples]
    return {"sample_size": len(samples),
            "brier_score": sum((row["probability"] - row["value"]) ** 2 for row in samples) / len(samples)
            if samples else None,
            "log_loss": "infinity" if 0 in losses else -sum(math.log(value) for value in losses) / len(losses)
            if losses else None, "bins": bins}


def _event_report(protocol, cohort, event, records, outcomes_by_id, as_of, truncated):
    rows, previous_end, previous_id = [], None, None
    for record in records:
        deadline = record["issued_at"] + event["horizon_days"] * DAY
        if event["event"] == "observable_endpoint":
            deadline = (deadline + 3599) // 3600 * 3600
        selected = previous_end is None or record["issued_at"] >= previous_end
        overlap = None if selected else previous_id
        if selected:
            previous_end, previous_id = deadline, record["assessment_id"]
        reasons = _reasons(protocol, record, cohort)
        probability, reason = _prediction(protocol, record, cohort, event)
        if reason:
            reasons.append(reason)
        appended = outcomes_by_id.get(record["assessment_id"], [])
        outcomes, rejected = select_outcomes(record, appended[:MAX_OUTCOMES], as_of)
        result = _resolve(record, outcomes, event, deadline, as_of)
        rows.append({"assessment_id": record["assessment_id"], "issued_at": record["issued_at"],
                     "deadline": deadline, "mature": deadline <= as_of, "probability": probability,
                     "nonoverlapping_selection": selected, "overlap_with": overlap,
                     **result, "observed_status": result["status"],
                     "status": "excluded" if reasons else result["status"], "exclusions": reasons,
                     "rejected_outcomes": rejected,
                     "outcomes_truncated": len(appended) > MAX_OUTCOMES})
    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("pending", "missing", "ambiguous", "resolved", "excluded")}
    counts.update(issued=len(rows), mature=sum(row["mature"] for row in rows),
                  nonoverlapping_selected=sum(row["nonoverlapping_selection"] for row in rows),
                  overlap_omitted=sum(not row["nonoverlapping_selection"] for row in rows))
    calibration = _calibration(rows)
    selected_mature = [row for row in rows if row["nonoverlapping_selection"] and row["mature"]]
    unresolved = sum(row["status"] != "resolved" for row in selected_mature)
    criterion = _criterion(protocol, calibration["sample_size"], len(selected_mature), unresolved,
                           truncated or any(row["outcomes_truncated"] for row in rows))
    return {"identity": deepcopy(event), "counts": counts, "events": rows, "calibration": calibration,
            "criterion": criterion}


def _criterion(protocol, sample_size, selected_mature, unresolved, truncated):
    criterion = protocol["declaration"]["criterion"]
    missing_fraction = unresolved / selected_mature if selected_mature else None
    met = (not truncated and protocol["declaration"]["mode"] == "prospective"
           and sample_size >= criterion["minimum_resolved_nonoverlapping"]
           and missing_fraction is not None and missing_fraction <= float(criterion["maximum_missing_fraction"]))
    return {**criterion, "selected_mature": selected_mature,
            "unresolved_selected_mature": unresolved, "missing_fraction": missing_fraction,
            "status": "descriptive_criterion_met" if met else "not_met", "predictive_edge_established": False}


def evaluate_validation(protocol, records, outcomes_by_id, *, as_of, cohort_ids=None, truncated_cohorts=()):
    """Evaluate frozen journal projections; no writes, market reads or fitting.

    At most 1000 originals, 100 cohorts and 100 origins per cohort per call.
    Cohort pages preserve separate native markets and avoid pooled asset scores.
    """
    protocol = validate_validation_protocol(protocol)
    utc_second(as_of)
    if as_of < protocol["frozen_at"]:
        raise ValueError("as_of precedes the protocol freeze")
    if not isinstance(records, list) or len(records) > MAX_RECORDS or not isinstance(outcomes_by_id, dict):
        raise ValueError("Use at most 1000 original records and grouped outcomes")
    cohorts = protocol["declaration"]["cohorts"]
    if cohort_ids is not None:
        declared = {cohort["cohort_id"] for cohort in cohorts}
        if not set(cohort_ids) <= declared:
            raise ValueError("Undeclared cohort ID")
        cohorts = [cohort for cohort in cohorts if cohort["cohort_id"] in cohort_ids]
    if len(cohorts) > 100:
        raise ValueError("Evaluate at most 100 cohorts per page")
    result, seen, duplicates = [], set(), 0
    originals = []
    for record in sorted(records, key=lambda row: (row["issued_at"], row["assessment_id"])):
        if record["assessment_id"] in seen:
            duplicates += 1
            continue
        seen.add(record["assessment_id"])
        if (record["issued_at"] <= as_of and protocol["declaration"]["starts_at"] <= record["issued_at"]
                < protocol["declaration"]["ends_at"] and record["payload"].get("kind") == "accumulation_assessment"):
            originals.append(record)
    for cohort in cohorts:
        candidates = [record for record in originals if _matches(record, cohort)]
        truncated = len(candidates) > 100 or cohort["cohort_id"] in truncated_cohorts
        selected = candidates[:100]
        plan_records, plan_excluded = [], []
        for record in selected:
            if not isinstance(record["payload"].get("plan"), dict):
                continue
            reasons = _reasons(protocol, record, cohort)
            plan = record["payload"]["plan"]
            config, baseline = plan.get("config", {}), protocol["declaration"]["baseline"]
            if (plan.get("rule_version") != baseline["rule_version"]
                    or config.get("fees") != baseline["fees"] or config.get("execution") != baseline["execution"]):
                reasons.append("declared_baseline_costs_or_rule_mismatch")
            if reasons:
                plan_excluded.append({"assessment_id": record["assessment_id"], "reasons": reasons})
            else:
                plan_records.append(record)
        plans = build_scorecard(plan_records, outcomes_by_id, as_of)
        plan_excluded.extend(plans["excluded_plans"])
        plan_truncation = truncated or any(len(outcomes_by_id.get(record["assessment_id"], [])) > MAX_OUTCOMES
                                          for record in plan_records)
        for plan in plans["plan_cohorts"]:
            plan["criterion"] = {}
            for horizon in ("90", "180"):
                mature = [row for row in plan["scenarios"] if row["independent_selection"]
                          and row["horizons"][horizon]["deadline"] <= as_of]
                qualified = sum(row["horizons"][horizon]["qualified"] for row in mature)
                plan["criterion"][horizon] = _criterion(protocol, qualified, len(mature),
                                                         len(mature) - qualified, plan_truncation)
        result.append({"cohort_id": cohort["cohort_id"],
                       "identity": {key: deepcopy(cohort[key]) for key in (*MARKET_FIELDS, "dataset_lineage",
                                                                             "rules", "model_version", "rule_version")},
                       "origins_truncated": truncated, "assessment_count": len(selected),
                       "events": [_event_report(protocol, cohort, event, selected, outcomes_by_id, as_of, truncated)
                                  for event in cohort["events"]],
                       "plan_cohorts": plans["plan_cohorts"], "excluded_plans": plan_excluded})
    return {"schema_version": 1, "protocol_version": protocol["protocol_version"],
            "protocol_hash": protocol["protocol_hash"], "frozen_at": protocol["frozen_at"], "as_of": as_of,
            "status": "descriptive_experimental", "mode": protocol["declaration"]["mode"],
            "duplicate_assessments": duplicates, "cohorts": result,
            "limitations": [
                "Protocol and observation clocks are declarations, not independently attested capture times.",
                "Nonoverlapping origins can remain correlated across time and assets; no independence claim is made.",
                "Minimum sample criteria are descriptive gates, not evidence of predictive edge or accumulation advantage.",
                "Missing, pending and excluded outcomes remain visible; resolved-only calibration can have selection bias.",
                "Prospective 90/180-day evidence requires actual elapsed time; fixtures cannot establish investment performance.",
                "Plan comparisons reuse complete funded execution qualification and paired regular purchases with identical cashflows."]}


def evaluate_stored_validation(store, protocol, *, as_of, cohort_page=0, page_size=10,
                               origins_per_cohort=100, record_budget=1000):
    """Read one bounded cohort page in a single SQLite read snapshot, without evidence.

    Pagination is across declared market cohorts, not overlapping origin batches.
    Earliest origins are selected before outcome availability; capped cohorts
    cannot pass the declared descriptive criterion. Missing stores stay absent.
    """
    protocol = validate_validation_protocol(protocol)
    utc_second(as_of)
    values = (cohort_page, page_size, origins_per_cohort, record_budget)
    if (any(type(value) is not int for value in values) or cohort_page < 0 or not 1 <= page_size <= 100
            or not 1 <= origins_per_cohort <= 100 or not 1 <= record_budget <= MAX_RECORDS
            or page_size * origins_per_cohort > record_budget):
        raise ValueError("Use a nonnegative cohort page and bounded page size, origin and record budgets")
    cohorts = protocol["declaration"]["cohorts"]
    page = cohorts[cohort_page * page_size:(cohort_page + 1) * page_size]
    snapshot = AssessmentJournal.open_existing(store).validation_records(
        page, starts_at=protocol["declaration"]["starts_at"], ends_at=protocol["declaration"]["ends_at"],
        as_of=as_of, origins_per_cohort=origins_per_cohort, record_budget=record_budget)
    result = evaluate_validation(protocol, snapshot.records, snapshot.outcomes, as_of=snapshot.as_of,
                                 cohort_ids=[cohort["cohort_id"] for cohort in page],
                                 truncated_cohorts=snapshot.selection["origins_truncated"])
    result["selection"] = {**snapshot.selection, "cohort_page": cohort_page, "page_size": page_size,
                           "total_cohorts": len(cohorts),
                           "next_cohort_page": cohort_page + 1 if (cohort_page + 1) * page_size < len(cohorts) else None}
    return result


def evaluate_validation_request(store, request, as_of):
    """Interpret the shared CLI/MCP request using the same read-only evaluator."""
    allowed = {"protocol", "cohort_page", "page_size", "origins_per_cohort", "record_budget"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Expected protocol and optional cohort paging/budget fields")
    if "protocol" not in request:
        raise ValueError("Prospective validation requires the frozen protocol artifact")
    values = {key: value for key, value in request.items() if key != "protocol"}
    return evaluate_stored_validation(store, request["protocol"], as_of=as_of, **values)
