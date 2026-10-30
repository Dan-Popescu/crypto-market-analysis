"""Bounded descriptive scorecards for immutable issues and appended outcomes.

No market reads, price interpolation, probability scoring or parameter fitting
occur here. A complete terminal valuation is an inspectable scenario; funded
execution evidence and original replay identity separately qualify a sample.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext

from .evidence import canonical_json, content_hash
from .plans import DAY, EXECUTION_EVIDENCE_RULE, RULE_VERSION, decimal_string, validate_config
from .prospective import assessment_chronology, select_outcomes, summarize_journal

_MARKET = ("source", "asset", "quote", "data_mode")
_FAMILIES = ("regular", "split", "deferred")
_METRICS = ("net_quantity", "new_purchases_all_in_cost", "total_wealth", "reserve",
            "maximum_drawdown", "maximum_waiting_seconds", "waiting_seconds_total",
            "cash_waiting_quote_days", "target_waiting_quote_days", "execution_waiting_quote_days",
            "cash_weighted_waiting_seconds", "amount_spent", "fees_quote", "fees_base")
_DELTAS = {"quantity_difference": "net_quantity",
           "acquisition_cost_difference": "new_purchases_all_in_cost",
           "terminal_wealth_difference": "total_wealth", "reserve_difference": "reserve"}


def _number(value):
    if value is None:
        return None
    if type(value) not in (str, int) or len(str(value)) > 100:
        raise ValueError("Invalid terminal numeric value")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid terminal numeric value") from exc
    if not number.is_finite() or abs(number.adjusted()) > 100:
        raise ValueError("Invalid terminal numeric value")
    return number


def _clock(value):
    return value if type(value) is int and value >= 0 else None


def _dispersion(values):
    values = [value for value in values if value is not None]
    if not values:
        return {"sample_size": 0, "mean": None, "minimum": None, "maximum": None,
                "population_standard_deviation": None}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {"sample_size": len(values), "mean": decimal_string(mean),
            "minimum": decimal_string(min(values)), "maximum": decimal_string(max(values)),
            "population_standard_deviation": decimal_string(variance.sqrt())}


def _rate(successes, total):
    if not total:
        return {"successes": 0, "sample_size": 0, "fraction": None, "wilson_95": None}
    n, z = Decimal(total), Decimal("1.959963984540054")
    p = Decimal(successes) / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * (p * (1 - p) / n + z * z / (4 * n * n)).sqrt() / denominator
    return {"successes": successes, "sample_size": total, "fraction": decimal_string(p),
            "wilson_95": {"lower": decimal_string(max(Decimal(0), center - radius)),
                          "upper": decimal_string(min(Decimal(1), center + radius))}}


def _nonoverlap(rows, start_key, end_key):
    """Select by declared time alone, before examining availability or returns."""
    previous_end, previous_id = None, None
    for row in sorted(rows, key=lambda item: (item[start_key], item["assessment_id"])):
        row["independent_selection"] = previous_end is None or row[start_key] >= previous_end
        row["overlap_with"] = None if row["independent_selection"] else previous_id
        if row["independent_selection"]:
            previous_end = row[end_key]
            previous_id = row["assessment_id"]
    return {"selected_assessment_ids": [row["assessment_id"] for row in rows if row["independent_selection"]],
            "overlap_omitted_assessment_ids": [row["assessment_id"] for row in rows if not row["independent_selection"]]}


def _events(summary):
    cohorts = {}
    for source in summary["cohorts"]:
        for row in source["events"]:
            identity = {**source["identity"], "temporality": row["temporality"]}
            cohort = cohorts.setdefault(canonical_json(identity), {"identity": identity, "events": []})
            cohort["events"].append(deepcopy(row))
    for cohort in cohorts.values():
        rows = cohort["events"]
        cohort["selection"] = _nonoverlap(rows, "issued_at", "deadline")
        cohort["counts"] = {key: sum(row["status"] == key for row in rows)
                            for key in ("resolved", "ambiguous", "missing", "pending", "excluded")}
        cohort["counts"].update(issued=len(rows), mature=sum(row["deadline"] <= summary["as_of"] for row in rows))
        selected = [row for row in rows if row["independent_selection"] and row["status"] != "excluded"]
        resolved = [row for row in selected if row["status"] == "resolved" and row["deadline"] <= summary["as_of"]]
        cohort["descriptive_rate"] = _rate(sum(row["value"] is True for row in resolved), len(resolved))
        cohort["independent_denominators"] = {
            "selected": len(selected), "mature": sum(row["deadline"] <= summary["as_of"] for row in selected),
            "resolved_mature": len(resolved), "overlap_omitted": sum(not row["independent_selection"] for row in rows)}
        cohort["probability_scoring"] = {"status": "comparability_not_established",
                                        "brier_score": None, "calibration": None}
    return [cohorts[key] for key in sorted(cohorts)]


def _policy_identity(config):
    policy = deepcopy(config)
    start = policy.pop("start_time")
    for field in ("contributions", "schedule"):
        for row in policy[field]:
            row["time"] -= start
    return policy


def _plan_original(record, chronology):
    frozen = record["payload"]["plan"]
    reasons = [reason for reason in chronology["exclusions"] if reason != "historical_evidence_cutoff"]
    try:
        config = validate_config(frozen.get("config"))
    except (ValueError, TypeError):
        return None, "unverified", [*reasons, "invalid_frozen_config"]
    start, issued = config["start_time"], record["issued_at"]
    temporality = chronology["temporality"]
    if start < issued:
        temporality = "retrospective" if start + 180 * DAY <= issued else "hybrid"
    reference = frozen.get("input_reference", {})
    original_reference = record["payload"].get("input_reference", {})
    if frozen.get("schema_version") != 1 or frozen.get("rule_version") != RULE_VERSION:
        reasons.append("unsupported_frozen_rule")
    if frozen.get("validation_mode") not in ("retrospective", "known_at"):
        reasons.append("invalid_frozen_validation_mode")
    if (any(reference.get(key) != config[key] or original_reference.get(key) != config[key] for key in _MARKET)
            or reference.get("dataset_lineage") != original_reference.get("dataset_lineage")):
        reasons.append("frozen_input_lineage_mismatch")
    cutoff, available = _clock(reference.get("as_of")), _clock(reference.get("available_at"))
    if (cutoff is None or cutoff > issued or available is None or available > cutoff
            or reference.get("as_of") != original_reference.get("as_of")):
        reasons.append("frozen_input_chronology_unverified")
    if (reference.get("configuration_hash") != content_hash(config)
            or not reference.get("content_hash")):
        reasons.append("frozen_input_identity_unverified")
    return config, temporality, reasons


def _replay_reasons(record, outcome, config):
    frozen, payload = record["payload"]["plan"], outcome["payload"]
    result = payload.get("plan", {})
    reference, replay = result.get("input_reference", {}), result.get("replay", {})
    cutoff, available = _clock(result.get("as_of")), _clock(reference.get("available_at"))
    reasons = []
    if (canonical_json(result.get("config")) != canonical_json(frozen.get("config"))
            or result.get("schema_version") != frozen.get("schema_version")
            or result.get("rule_version") != frozen.get("rule_version")
            or result.get("validation_mode") != frozen.get("validation_mode")):
        reasons.append("frozen_policy_changed")
    if (cutoff is None or cutoff != _clock(payload.get("as_of")) or cutoff > outcome["observed_at"]
            or reference.get("as_of") != cutoff or available is None or available > cutoff):
        reasons.append("plan_outcome_chronology_invalid")
    if (any(reference.get(key) != config[key] or result.get(key) != config[key] for key in _MARKET)
            or reference.get("dataset_lineage") != frozen.get("input_reference", {}).get("dataset_lineage")
            or reference.get("configuration_hash") != content_hash(config) or not reference.get("content_hash")):
        reasons.append("plan_outcome_lineage_mismatch")
    if (outcome.get("assessment_id") != record["assessment_id"]
            or payload.get("original_input_reference") != record["payload"].get("input_reference")
            or replay.get("assessment_id") != record["assessment_id"]
            or replay.get("original_content_hash") != record.get("content_hash")
            or not record.get("content_hash")
            or replay.get("original_input_reference") != frozen.get("input_reference")
            or replay.get("frozen_rule_version") != frozen.get("rule_version")
            or replay.get("future_price_selection") != "first_observed_version_fully_post_issuance"
            or replay.get("boundary_policy") != "exclude_bars_straddling_actual_issuance"
            or any(not replay.get(key) for key in ("pinned_content_hash", "future_content_hash"))
            or any(_clock(replay.get(key)) is None for key in ("pinned_candle_versions", "future_candle_versions"))):
        reasons.append("original_replay_identity_unverified")
    return reasons


def _close(left, right):
    # The ledger rounds Decimal operations at precision 28; allow only its last digits.
    return abs(left - right) <= Decimal("1e-25") * max(Decimal(1), abs(left), abs(right))


def _terminals(result, config, days):
    deadline = config["start_time"] + days * DAY
    terminals = {family: result.get("plans", {}).get(family, {}).get("terminals", {}).get(str(days), {})
                 for family in _FAMILIES}
    if not all(row.get("status") == "complete" for row in terminals.values()):
        return None, []
    reasons = []
    baseline = terminals["regular"]
    try:
        price = _number(baseline.get("price"))
        deposits = sum(Decimal(row["amount"]) for row in config["contributions"] if row["time"] <= deadline)
        if price is None or price <= 0:
            raise ValueError("Missing valuation price")
        for row in terminals.values():
            if (row.get("time") != deadline or row.get("price") != baseline.get("price")
                    or row.get("price_time") != baseline.get("price_time")
                    or _clock(row.get("price_time")) is None or row["price_time"] > deadline
                    or deadline - row["price_time"] > config["execution"]["max_price_age_seconds"]
                    or _number(row.get("contributions")) != deposits):
                raise ValueError("Unpaired valuation or cashflows")
            required = {name: _number(row.get(name)) for name in (*_METRICS, "holdings_quantity")}
            if any(value is None for name, value in required.items()
                   if name not in ("new_purchases_all_in_cost", "cash_weighted_waiting_seconds")):
                raise ValueError("Missing terminal metrics")
            if any(value < 0 for value in required.values() if value is not None) or required["maximum_drawdown"] > 1:
                raise ValueError("Invalid terminal metric range")
            if not _close(required["total_wealth"], required["reserve"] + required["holdings_quantity"] * price):
                raise ValueError("Wealth must include cash reserve")
            if not _close(required["holdings_quantity"], Decimal(config["starting_holdings"]["quantity"]) + required["net_quantity"]):
                raise ValueError("Holdings mismatch")
            quantity, cost = required["net_quantity"], required["new_purchases_all_in_cost"]
            if ((quantity == 0 and cost is not None)
                    or (quantity > 0 and (cost is None or not _close(
                        cost, (required["amount_spent"] + required["fees_quote"]) / quantity)))):
                raise ValueError("Acquisition cost mismatch")
            coverage = row.get("execution_coverage", {})
            if (coverage.get("qualified") is not True or coverage.get("rule") != EXECUTION_EVIDENCE_RULE
                    or coverage.get("reasons") != []
                    or any(coverage.get(key) != 0 for key in
                           ("missing_bars", "missing_activation_anchors", "unobservable_execution_windows"))
                    or _clock(coverage.get("expected_bars")) is None
                    or _clock(coverage.get("observed_bars")) is None
                    or coverage.get("expected_bars") != coverage.get("observed_bars")):
                reasons.append("funded_execution_evidence_unqualified")
    except (ValueError, TypeError, InvalidOperation):
        return None, ["invalid_paired_terminal_accounting"]
    return terminals, sorted(set(reasons))


def _horizon(record, outcomes, config, days, as_of, original_reasons):
    deadline = config["start_time"] + days * DAY
    row = {"deadline": deadline, "status": "pending" if as_of < deadline else "missing",
           "outcome_hash": None, "observed_at": None, "qualified": False,
           "qualification_reasons": list(original_reasons), "rejected_outcomes": [], "plans": None}
    for outcome in outcomes:
        if not isinstance(outcome["payload"].get("plan"), dict):
            continue
        reasons = _replay_reasons(record, outcome, config)
        result = outcome["payload"]["plan"]
        cutoff = _clock(result.get("as_of"))
        if cutoff is None or cutoff < deadline or as_of < deadline:
            continue
        terminals, terminal_reasons = _terminals(result, config, days)
        if reasons or (terminals is None and terminal_reasons):
            row["rejected_outcomes"].append({"outcome_hash": outcome.get("content_hash"),
                                              "reasons": [*reasons, *terminal_reasons]})
            continue
        if terminals is None:
            continue
        row.update(status="complete", outcome_hash=outcome.get("content_hash"),
                   observed_at=outcome["observed_at"], plans=deepcopy(terminals),
                   qualified=not original_reasons and not terminal_reasons,
                   qualification_reasons=[*original_reasons, *terminal_reasons])
        break
    return row


def _metrics(rows, days, family):
    samples = [row["horizons"][days]["plans"] for row in rows
               if row["independent_selection"] and row["horizons"][days]["qualified"]]
    metrics = {"sample_size": len(samples)}
    for name in _METRICS:
        metrics[name] = _dispersion([_number(plans[family][name]) for plans in samples])
    for name, field in _DELTAS.items():
        differences = []
        for plans in samples:
            left, right = _number(plans[family][field]), _number(plans["regular"][field])
            differences.append(left - right if left is not None and right is not None else None)
        metrics[name] = _dispersion(differences)
    losses = sum(_number(plans[family]["total_wealth"]) < _number(plans["regular"]["total_wealth"])
                 for plans in samples)
    metrics["underperformance"] = _rate(losses, len(samples))
    return metrics


def build_scorecard(records: list[dict], outcomes_by_id: dict[str, list[dict]], as_of: int) -> dict:
    """Summarize at most 100 originals and 256 appends per original at a cutoff.

    First complete identity-valid outcomes by observed time/hash are retained,
    even if their execution evidence is unqualified. Independent selections use
    declared clocks alone and never substitute a later profitable scenario.
    """
    with localcontext() as context:
        context.prec = 28
        summary = summarize_journal(records, outcomes_by_id, as_of)
        cohorts, seen, excluded = {}, set(), []
        for record in records[:100]:
            identity, payload = record["assessment_id"], record.get("payload", {})
            if identity in seen or record["issued_at"] > as_of or payload.get("kind") != "accumulation_assessment":
                continue
            seen.add(identity)
            if not isinstance(payload.get("plan"), dict):
                continue
            chronology = assessment_chronology(record)
            config, temporality, reasons = _plan_original(record, chronology)
            if config is None:
                excluded.append({"assessment_id": identity, "reasons": reasons})
                continue
            frozen = payload["plan"]
            cohort_identity = {**{key: config[key] for key in _MARKET}, "temporality": temporality,
                               "dataset_lineage": frozen.get("input_reference", {}).get("dataset_lineage"),
                               "rules": payload.get("rules", {}), "rule_version": frozen.get("rule_version"),
                               "validation_mode": frozen.get("validation_mode"),
                               "policy_and_cashflows": _policy_identity(config)}
            cohort = cohorts.setdefault(canonical_json(cohort_identity), {"identity": cohort_identity, "scenarios": []})
            outcomes, rejected = select_outcomes(record, outcomes_by_id.get(identity, [])[:256], as_of)
            row = {"assessment_id": identity, "assessment_hash": record.get("content_hash"),
                   "issued_at": record["issued_at"], "start_time": config["start_time"],
                   "end_time": config["start_time"] + 180 * DAY, "chronology": chronology,
                   "exclusions": reasons, "rejected_outcomes": rejected,
                   "horizons": {str(days): _horizon(record, outcomes, config, days, as_of, reasons)
                                for days in (90, 180)}}
            cohort["scenarios"].append(row)
        for cohort in cohorts.values():
            rows = cohort["scenarios"]
            cohort["selection"] = _nonoverlap(rows, "start_time", "end_time")
            cohort["counts"] = {}
            cohort["metrics"] = {}
            for days in ("90", "180"):
                cohort["counts"][days] = {
                    "issued": len(rows), "mature": sum(as_of >= row["horizons"][days]["deadline"] for row in rows),
                    **{status: sum(row["horizons"][days]["status"] == status for row in rows)
                       for status in ("complete", "missing", "pending")},
                    "excluded": sum(bool(row["exclusions"]) for row in rows),
                    "scenario_sample_size": sum(row["horizons"][days]["status"] == "complete" for row in rows),
                    "qualified_before_overlap": sum(row["horizons"][days]["qualified"] for row in rows),
                    "unqualified_scenarios": sum(row["horizons"][days]["status"] == "complete"
                                                 and not row["horizons"][days]["qualified"] for row in rows),
                    "sample_size": sum(row["horizons"][days]["qualified"] and row["independent_selection"] for row in rows),
                    "selected": sum(row["independent_selection"] for row in rows),
                    "selected_missing": sum(row["independent_selection"] and row["horizons"][days]["status"] == "missing" for row in rows),
                    "selected_pending": sum(row["independent_selection"] and row["horizons"][days]["status"] == "pending" for row in rows),
                    "overlap_omitted": sum(not row["independent_selection"] for row in rows)}
                cohort["metrics"][days] = {family: _metrics(rows, days, family) for family in ("split", "deferred")}
        return {"schema_version": 1, "rule_version": "journal_scorecard_v1", "as_of": as_of,
                "status": "descriptive_experimental", "assessment_count": summary["assessment_count"],
                "duplicate_assessments": summary["duplicate_assessments"],
                "ignored_assessments": summary["ignored_assessments"], "truncation": summary["truncation"],
                "event_cohorts": _events(summary), "plan_cohorts": [cohorts[key] for key in sorted(cohorts)],
                "excluded_plans": excluded,
                "protocol": {"outcome_selection": "first_eligible_observed_time_then_content_hash",
                             "plan_selection": "first_complete_identity_valid_even_when_execution_unqualified",
                             "independent_selection": "earliest_declared_start_then_assessment_id_greedy_nonoverlap",
                             "plan_spacing_days": 180, "selection_before_outcome_availability": True,
                             "wealth": "holdings_at_shared_native_quote_mark_plus_all_uninvested_reserve",
                             "cash_reserve_yield": "0", "probability_scoring": "comparability_not_established"},
                "limitations": [
                    "True prospective 90/180-day samples require actual elapsed time and eligible appended observations.",
                    "Synthetic, retrospective and hybrid scenarios do not establish prospective investment performance.",
                    "Non-overlapping windows can remain dependent; Wilson intervals assume independence and are descriptive only.",
                    "Missing and pending outcomes remain in denominators; resolved-only rates can have missing-outcome bias.",
                    "Observation timestamps are journal declarations; this projection does not independently attest capture time or rerun archived evidence.",
                    "Anchor-relative forecast probabilities are not scored against issuance-relative outcome conventions.",
                    "Configured execution costs, zero reserve yield and incomplete intrabar drawdown are simulation assumptions.",
                    "No calibration, predictive edge, fitted policy or accumulation advantage is established."]}
