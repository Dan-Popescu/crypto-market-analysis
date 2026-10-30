"""Synthetic frozen-protocol examples establish mechanics, never market edge."""

from copy import deepcopy

import pytest

from crypto_market_analysis.journal import AssessmentJournal
from crypto_market_analysis.plans import RULE_VERSION
from crypto_market_analysis.prospective_validation import (
    build_validation_predictions,
    evaluate_stored_validation,
    evaluate_validation,
)
from crypto_market_analysis.storage import Store
from crypto_market_analysis.validation_protocol import (
    freeze_validation_protocol,
    validate_validation_protocol,
)

DAY = 86400


def declaration(assets=("BTC",)):
    return {"name": "Synthetic mechanics example", "mode": "prospective", "starts_at": DAY, "ends_at": 300 * DAY,
            "criterion": {"minimum_resolved_nonoverlapping": 2, "maximum_missing_fraction": "0.1"},
            "baseline": {"family": "regular", "reserve_yield": "0", "rule_version": RULE_VERSION,
                         "fees": {"rate": "0", "fixed": "0", "asset": "quote"},
                         "execution": {"timeframe": "1d", "max_price_age_seconds": DAY,
                                       "spread_bps": "0", "slippage_bps": "0", "quantity_step": "0.00000001",
                                       "price_step": "0.00000001", "assumption": "historical_configured_costs"}},
            "cohorts": [{"cohort_id": asset, "source": "test", "asset": asset, "quote": "EUR",
                         "data_mode": "import", "dataset_lineage": {"name": "synthetic-research"},
                         "model_version": "purchase_windows_v1", "rule_version": "issuance_hourly_v1",
                         "rules": {"purchase_windows": 1},
                         "events": [{"event": "endpoint_below_anchor", "convention": "exact_deadline_v1",
                                     "time_anchor": "issuance", "horizon_days": 1, "threshold": None}]}
                        for asset in assets]}


def frozen(assets=("BTC",), *, frozen_at=0):
    return freeze_validation_protocol(declaration(assets), frozen_at=frozen_at)


def original(protocol, identity="a", *, issued=DAY, asset="BTC", probability=0.75):
    cohort = next(cohort for cohort in protocol["declaration"]["cohorts"] if cohort["asset"] == asset)
    reference = {key: deepcopy(cohort[key]) for key in ("source", "asset", "quote", "data_mode", "dataset_lineage")}
    reference.update(as_of=issued, available_at=issued)
    prediction = {**cohort["events"][0], "probability": probability, "protocol_hash": protocol["protocol_hash"],
                  "model_version": cohort["model_version"], "rule_version": cohort["rule_version"],
                  "window_start": issued, "window_end": issued + DAY, "anchor_time": issued, "anchor_price": 10}
    return {"assessment_id": identity, "issued_at": issued, "content_hash": "issue-" + identity,
            "payload": {"kind": "accumulation_assessment", "input_reference": reference,
                        "rules": deepcopy(cohort["rules"]), "validation_protocol_hash": protocol["protocol_hash"],
                        "validation_predictions": [prediction], "windows": {"anchor_time": issued, "anchor_price": 10}}}


def outcome(record, *, value=True, observed_at=None, digest="first", anchor=None):
    issued = record["issued_at"]
    clock = issued + DAY if observed_at is None else observed_at
    return {"assessment_id": record["assessment_id"], "content_hash": digest, "observed_at": clock,
            "payload": {"as_of": clock, "input_reference": deepcopy(record["payload"]["input_reference"]),
                        "windows": [{"horizon_days": 1, "as_of": clock, "issued_at": issued,
                                     "anchor_time": issued if anchor is None else anchor, "window_end": issued + DAY, "anchor_price": 10,
                                     "endpoint_below_anchor": {"state": "known_true" if value else "known_false"}}]}}


def event(result, cohort=0):
    return result["cohorts"][cohort]["events"][0]


def test_protocol_is_content_addressed_defensive_and_tampering_is_rejected():
    config = declaration()
    protocol = freeze_validation_protocol(config, frozen_at=0)
    config["cohorts"][0]["asset"] = "ETH"
    assert protocol["declaration"]["cohorts"][0]["asset"] == "BTC"
    assert validate_validation_protocol(protocol) == protocol
    protocol["declaration"]["criterion"]["minimum_resolved_nonoverlapping"] = 3
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_validation_protocol(protocol)


def test_scores_known_at_first_outcome_and_visible_denominators():
    protocol = frozen()
    first, second = original(protocol), original(protocol, "b", issued=3 * DAY, probability=0.25)
    future = outcome(second, value=False)
    revised = outcome(first, value=False, digest="revision", observed_at=4 * DAY)
    report = evaluate_validation(protocol, [first, second], {"a": [revised, outcome(first)], "b": [future]},
                                 as_of=4 * DAY)
    row = event(report)
    assert row["counts"]["issued"] == row["counts"]["resolved"] == 2
    assert row["calibration"]["sample_size"] == 2
    assert row["calibration"]["brier_score"] == pytest.approx(0.0625)
    assert row["calibration"]["log_loss"] == pytest.approx(0.28768207245)
    assert row["events"][0]["outcome_hash"] == "first"
    assert row["criterion"]["status"] == "descriptive_criterion_met"
    assert row["criterion"]["predictive_edge_established"] is False
    earlier = event(evaluate_validation(protocol, [first, second], {"a": [outcome(first)], "b": [future]},
                                        as_of=3 * DAY))
    assert earlier["counts"]["resolved"] == earlier["counts"]["pending"] == 1
    assert earlier["criterion"]["status"] == "not_met"


def test_late_protocol_unbound_original_and_historical_training_cannot_be_prospective():
    protocol = frozen(frozen_at=2 * DAY)
    record = original(protocol)
    row = event(evaluate_validation(protocol, [record], {"a": [outcome(record)]}, as_of=3 * DAY))
    assert row["counts"]["excluded"] == 1
    assert "protocol_frozen_after_issue" in row["events"][0]["exclusions"]
    protocol = frozen()
    record = original(protocol)
    record["payload"]["input_reference"]["as_of"] -= 1
    del record["payload"]["validation_protocol_hash"]
    row = event(evaluate_validation(protocol, [record], {}, as_of=3 * DAY))
    assert set(row["events"][0]["exclusions"]) >= {"historical_evidence_cutoff", "protocol_not_bound_at_issue"}
    assert row["calibration"]["sample_size"] == 0


def test_anchor_relative_prediction_and_misaligned_outcome_are_never_scored():
    protocol = frozen()
    record = original(protocol)
    row = event(evaluate_validation(protocol, [record], {"a": [outcome(record, anchor=DAY - 3600)]}, as_of=3 * DAY))
    assert row["counts"]["missing"] == 1
    record["payload"]["validation_predictions"][0]["anchor_time"] -= 3600
    row = event(evaluate_validation(protocol, [record], {"a": [outcome(record)]}, as_of=3 * DAY))
    assert row["events"][0]["exclusions"] == ["prediction_time_anchor_mismatch"]
    assert row["calibration"]["brier_score"] is None


def test_nonoverlap_selection_precedes_missingness_and_does_not_substitute_winner():
    protocol = frozen()
    records = [original(protocol), original(protocol, "b", issued=DAY + 1), original(protocol, "c", issued=3 * DAY)]
    row = event(evaluate_validation(protocol, records, {"b": [outcome(records[1])], "c": [outcome(records[2])]},
                                    as_of=4 * DAY))
    assert row["counts"]["nonoverlapping_selected"] == 2
    assert row["counts"]["overlap_omitted"] == 1
    assert row["calibration"]["sample_size"] == 1
    assert row["criterion"]["missing_fraction"] == 0.5
    assert row["criterion"]["status"] == "not_met"


def test_helper_derives_only_aligned_actual_conditional_estimates():
    protocol = frozen()
    record = original(protocol)
    windows = {"schema_version": 1, "issued_at": DAY, "anchor_time": DAY, "anchor_price": 10, "stale": False,
               "windows": {"1": {"0.05": {"status": "experimental", "threshold": 0.05,
                   "historical_alignment": "anchor_relative_complete_24h_bars", "issuance_alignment_matches": True,
                   "estimate_anchor_time": DAY, "estimate_window_end": 2 * DAY, "purchase_window_end": 2 * DAY,
                   "conditional_frequencies": {"endpoint_below_anchor": 0.6}}}}}
    predictions = build_validation_predictions(protocol, record["payload"]["input_reference"], windows,
                                                record["payload"]["rules"], DAY)
    assert predictions[0]["probability"] == 0.6
    windows["anchor_time"] -= 1
    assert build_validation_predictions(protocol, record["payload"]["input_reference"], windows,
                                         record["payload"]["rules"], DAY) == []


def test_pending_excluded_and_missing_survive_and_zero_probability_loss_is_explicit():
    protocol = frozen()
    first = original(protocol, probability=0)
    record = original(protocol, "b", issued=3 * DAY)
    report = evaluate_validation(protocol, [first, record], {"a": [outcome(first)]}, as_of=4 * DAY)
    row = event(report)
    assert row["counts"]["missing"] == row["counts"]["resolved"] == 1
    assert row["calibration"]["log_loss"] == "infinity"
    assert row["criterion"]["status"] == "not_met"


def test_store_reads_cohort_pages_readonly_and_does_not_create_missing_store(tmp_path):
    protocol = frozen(tuple(f"ASSET{index}" for index in range(1000)))
    missing = Store(tmp_path / "absent.sqlite", initialize=False)
    empty = evaluate_stored_validation(missing, protocol, as_of=3 * DAY)
    assert not missing.path.exists()
    assert empty["selection"]["next_cohort_page"] == 1
    assert len(empty["cohorts"]) == 10
    store = Store(tmp_path / "journal.sqlite")
    journal = AssessmentJournal(store)
    record = original(protocol, asset="ASSET999")
    issued = journal.issue("a", record["payload"], {"large_frozen_evidence": [1, 2, 3]}, DAY)
    observed = outcome(issued)
    journal.append_outcome(issued["assessment_id"], "resolved", observed["payload"], 2 * DAY)
    before = store.path.read_bytes()
    report = evaluate_stored_validation(store, protocol, as_of=3 * DAY, cohort_page=99)
    assert report["selection"]["selected_records"] == 1
    assert report["selection"]["next_cohort_page"] is None
    assert event(report, cohort=9)["counts"]["resolved"] == 1
    assert store.path.read_bytes() == before
    assert report["selection"]["evidence_loaded"] is False


def test_per_cohort_origin_cap_cannot_pass_criterion_and_budget_is_enforced(tmp_path):
    protocol = frozen()
    journal = AssessmentJournal(Store(tmp_path / "journal.sqlite"))
    for index in range(3):
        record = original(protocol, str(index), issued=(index * 2 + 1) * DAY)
        issued = journal.issue(str(index), record["payload"], {}, record["issued_at"])
        appended = outcome(issued)
        journal.append_outcome(issued["assessment_id"], "resolved", appended["payload"], appended["observed_at"])
    report = evaluate_stored_validation(journal.store, protocol, as_of=10 * DAY,
                                        page_size=1, origins_per_cohort=2, record_budget=2)
    assert report["cohorts"][0]["origins_truncated"] is True
    assert event(report)["calibration"]["sample_size"] == 2
    assert event(report)["criterion"]["status"] == "not_met"
    with pytest.raises(ValueError, match="budget"):
        evaluate_stored_validation(journal.store, protocol, as_of=10 * DAY, page_size=100, record_budget=1000)


def test_retrospective_and_native_cohorts_are_separate():
    config = declaration(("BTC", "ETH"))
    config["mode"] = "retrospective"
    protocol = freeze_validation_protocol(config, frozen_at=0)
    first, second = original(protocol), original(protocol, "b", asset="ETH")
    report = evaluate_validation(protocol, [first, second], {"a": [outcome(first)], "b": [outcome(second)]}, as_of=3 * DAY)
    assert len(report["cohorts"]) == 2
    assert report["mode"] == "retrospective"
    assert all(event(report, index)["counts"]["excluded"] == 1 for index in (0, 1))


@pytest.mark.parametrize("mutation", [
    lambda config: config["cohorts"][0]["events"][0].update(time_anchor="historical_anchor"),
    lambda config: config["baseline"].update(reserve_yield="0.03"),
    lambda config: config["cohorts"].append(deepcopy(config["cohorts"][0])),
    lambda config: config["criterion"].update(minimum_resolved_nonoverlapping=0),
])
def test_invalid_protocol_declarations_are_rejected(mutation):
    config = declaration()
    mutation(config)
    with pytest.raises(ValueError):
        freeze_validation_protocol(config, frozen_at=0)


def test_plan_baseline_uses_existing_funded_execution_validation_without_probability():
    from test_scorecards import appended
    from test_scorecards import original as plan_original

    protocol = frozen()
    record = plan_original()
    record["payload"]["validation_protocol_hash"] = protocol["protocol_hash"]
    sparse = appended(record, sparse=True)
    complete = appended(record, days=182, digest="later-complete")
    report = evaluate_validation(protocol, [record], {"a": [sparse, complete]}, as_of=183 * DAY)
    cohort = report["cohorts"][0]
    assert cohort["events"][0]["calibration"]["sample_size"] == 0
    plan = cohort["plan_cohorts"][0]
    assert plan["counts"]["180"]["scenario_sample_size"] == 1
    assert plan["counts"]["180"]["sample_size"] == 0
    report = evaluate_validation(protocol, [record], {"a": [complete]}, as_of=183 * DAY)
    plan = report["cohorts"][0]["plan_cohorts"][0]
    assert plan["counts"]["180"]["sample_size"] == 1
    assert plan["metrics"]["180"]["deferred"]["terminal_wealth_difference"]["sample_size"] == 1
    record["payload"]["plan"]["config"]["fees"]["rate"] = "0.02"
    report = evaluate_validation(protocol, [record], {"a": [complete]}, as_of=183 * DAY)
    assert report["cohorts"][0]["plan_cohorts"] == []
    assert report["cohorts"][0]["excluded_plans"][0]["reasons"] == ["declared_baseline_costs_or_rule_mismatch"]


def test_outcomes_after_cutoff_foreign_lineage_and_wrong_versions_are_excluded():
    protocol = frozen()
    record = original(protocol)
    foreign = outcome(record)
    foreign["payload"]["input_reference"]["quote"] = "USD"
    row = event(evaluate_validation(protocol, [record], {"a": [foreign, outcome(record, observed_at=4 * DAY)]},
                                    as_of=3 * DAY))
    assert row["counts"]["missing"] == 1
    assert row["events"][0]["rejected_outcomes"][0]["reason"] == "outcome_lineage_mismatch"
    record["payload"]["validation_predictions"][0]["model_version"] = "changed-after-issue"
    row = event(evaluate_validation(protocol, [record], {}, as_of=3 * DAY))
    assert row["events"][0]["exclusions"] == ["prediction_version_mismatch"]


def test_duplicate_records_and_outcome_truncation_cannot_inflate_evidence():
    protocol = frozen()
    records = [original(protocol), original(protocol, "b", issued=3 * DAY)]
    appends = {record["assessment_id"]: [outcome(record, digest=str(index)) for index in range(257)]
               for record in records}
    report = evaluate_validation(protocol, records + [records[0]], appends, as_of=5 * DAY)
    row = event(report)
    assert report["duplicate_assessments"] == 1
    assert row["counts"]["resolved"] == 2
    assert row["events"][0]["outcomes_truncated"] is True
    assert row["criterion"]["status"] == "not_met"


def test_default_live_store_null_lineage_is_explicit_and_queryable(tmp_path):
    config = declaration()
    config["cohorts"][0].update(data_mode="live", dataset_lineage=None)
    protocol = freeze_validation_protocol(config, frozen_at=0)
    record = original(protocol)
    del record["payload"]["input_reference"]["dataset_lineage"]
    journal = AssessmentJournal(Store(tmp_path / "live.sqlite"))
    issued = journal.issue("live", record["payload"], {}, DAY)
    appended = outcome(issued)
    journal.append_outcome(issued["assessment_id"], "resolved", appended["payload"], 2 * DAY)
    report = evaluate_stored_validation(journal.store, protocol, as_of=3 * DAY)
    assert event(report)["counts"]["resolved"] == 1
    assert report["cohorts"][0]["identity"]["dataset_lineage"] is None


def test_changed_anchor_price_and_late_outcome_availability_cannot_resolve():
    protocol = frozen()
    record = original(protocol)
    changed = outcome(record)
    changed["payload"]["windows"][0]["anchor_price"] = 11
    report = evaluate_validation(protocol, [record], {"a": [changed]}, as_of=3 * DAY)
    assert event(report)["counts"]["missing"] == 1
    late = outcome(record)
    late["payload"]["input_reference"]["available_at"] = 4 * DAY
    report = evaluate_validation(protocol, [record], {"a": [late]}, as_of=3 * DAY)
    assert event(report)["counts"]["missing"] == 1


def test_shareable_example_freezes_without_claiming_any_elapsed_evidence():
    import json
    from pathlib import Path

    config = json.loads((Path(__file__).parents[1] / "examples" / "validation-protocol.json").read_text())
    protocol = freeze_validation_protocol(config, frozen_at=1790000000)
    report = evaluate_validation(protocol, [], {}, as_of=1790000000)
    assert protocol["declaration"]["starts_at"] == 1798761600
    assert report["cohorts"][0]["assessment_count"] == 0
    assert all(row["criterion"]["status"] == "not_met" for row in report["cohorts"][0]["events"])
