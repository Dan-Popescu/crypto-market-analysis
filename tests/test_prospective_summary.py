"""Journal denominators preserve issuance clocks, lineage and appended evidence."""

from copy import deepcopy

import pytest

from crypto_market_analysis.prospective import summarize_journal

DAY = 86400


def assessment(identity="a", *, mode="import", quote="EUR", issued=10000, cutoff=None):
    reference = {"source": "venue", "asset": "BTC", "quote": quote, "data_mode": mode,
                 "as_of": issued if cutoff is None else cutoff,
                 "dataset_lineage": {"name": "research", "source": "venue", "data_mode": mode}}
    return {"assessment_id": identity, "issued_at": issued, "content_hash": "issue-" + identity,
            "payload": {"kind": "accumulation_assessment", "input_reference": reference,
                        "rules": {"purchase_windows": 1},
                        "outcome_protocol": {"endpoint_convention": "hourly_after_deadline_v1"},
                        "windows": {"windows": {"1": {"0.05": {}}}}},
            "evidence": {"candles": [{"observed_at": 9000, "close_time": 7200}]}}


def observed(record, *, clock=None, state="known_true", digest="first"):
    clock = record["issued_at"] + DAY + 3600 if clock is None else clock
    event = {"state": state, "value": True if state == "known_true" else False
             if state == "known_false" else None}
    return {"observed_at": clock, "content_hash": digest,
            "payload": {"as_of": clock, "input_reference": record["payload"]["input_reference"],
                        "windows": [{"horizon_days": 1, "threshold": 0.05,
                                     "as_of": clock, "endpoint_below_anchor": event,
                                     "below_anchor_touch": event, "observable_endpoint": event}]}}


def event_cohort(result, event):
    return next(row for row in result["cohorts"] if row["identity"]["event"] == event)


def test_repeated_outcome_appends_count_one_issue_event_and_pin_first_definite():
    record = assessment()
    first = observed(record)
    revision = observed(record, clock=first["observed_at"] + 1, state="known_false", digest="revision")
    result = summarize_journal([record], {"a": [revision, first, first]}, 200000)
    cohort = event_cohort(result, "endpoint_below_anchor")
    assert cohort["counts"]["issued"] == cohort["counts"]["resolved"] == 1
    assert cohort["events"][0]["outcome_hash"] == "first"
    assert cohort["events"][0]["value"] is True


def test_missing_outcomes_stay_in_mature_denominator():
    result = summarize_journal([assessment()], {}, 200000)
    for cohort in result["cohorts"]:
        assert cohort["counts"]["issued"] == cohort["counts"]["mature"] == 1
        assert cohort["counts"]["missing"] == 1
        assert cohort["counts"]["resolved"] == 0


def test_native_quote_and_mode_and_dataset_lineage_never_pool():
    records = [assessment("a"), assessment("b", quote="USD"), assessment("c", mode="live")]
    fourth = assessment("d")
    fourth["payload"]["input_reference"]["dataset_lineage"]["name"] = "other"
    result = summarize_journal(records + [fourth], {}, 200000)
    assert len(result["cohorts"]) == 12
    assert all(row["counts"]["issued"] == 1 for row in result["cohorts"])


def test_historical_plan_cannot_become_prospective_by_issuing_today():
    record = assessment(issued=100 * DAY)
    record["payload"]["plan"] = {"rule_version": "accumulation-v2", "config": {"start_time": DAY}}
    result = summarize_journal([record], {}, 101 * DAY)
    assert result["plans"][0]["temporality"] == "hybrid"
    assert result["plans"][0]["qualified_performance_samples"] == 0
    assert result["plans"][0]["terminals"]["180"]["status"] == "pending"


def test_imported_training_available_before_future_forecast_qualifies():
    record = assessment()
    result = summarize_journal([record], {"a": [observed(record)]}, 200000)
    for cohort in result["cohorts"]:
        assert cohort["events"][0]["temporality"] == "prospective"
        assert cohort["counts"]["excluded"] == 0
        assert cohort["counts"]["resolved"] == 1


def test_live_does_not_override_actual_training_availability_or_historical_cutoff():
    record = assessment(mode="live")
    record["evidence"]["candles"][0]["observed_at"] = 10001
    result = summarize_journal([record], {"a": [observed(record)]}, 200000)
    assert all(row["counts"]["excluded"] == 1 for row in result["cohorts"])
    historical = assessment(cutoff=9000)
    result = summarize_journal([historical], {}, 200000)
    assert all(row["events"][0]["temporality"] == "retrospective" for row in result["cohorts"])


def test_summary_as_of_ignores_subsequent_appends_and_issuance():
    record = assessment()
    result = summarize_journal([record], {"a": [observed(record)]}, record["issued_at"] + 1)
    assert all(row["counts"]["pending"] == 1 for row in result["cohorts"])
    before = summarize_journal([record], {}, record["issued_at"] - 1)
    assert before["assessment_count"] == 0


def test_legacy_records_keep_exact_endpoint_and_touch_only_and_overlaps_are_explicit():
    record = assessment()
    del record["payload"]["outcome_protocol"]
    second = deepcopy(record)
    second.update(assessment_id="b", issued_at=record["issued_at"] + 1)
    result = summarize_journal([record, second], {}, 200000)
    assert len(result["cohorts"]) == 2
    assert all(row["overlapping_pairs"] == 1 for row in result["cohorts"])


def test_bounds_are_explicit_and_duplicate_originals_do_not_duplicate_events():
    record = assessment()
    result = summarize_journal([record, record], {}, 200000)
    assert result["assessment_count"] == 1
    assert result["duplicate_assessments"] == 1


def test_available_at_projection_and_unverifiable_legacy_projection():
    record = assessment()
    del record["evidence"]
    result = summarize_journal([record], {}, 200000)
    assert all(row["events"][0]["exclusions"] == ["availability_unverified"] for row in result["cohorts"])
    record["payload"]["input_reference"]["available_at"] = 9000
    result = summarize_journal([record], {}, 200000)
    assert all(row["counts"]["missing"] == 1 for row in result["cohorts"])


def test_ambiguous_appends_can_resolve_and_foreign_lineage_is_rejected():
    record = assessment()
    ambiguous = observed(record, digest="ambiguous", state="ambiguous")
    definite = observed(record, clock=ambiguous["observed_at"] + 1)
    foreign = deepcopy(definite)
    foreign["payload"]["input_reference"] = {**foreign["payload"]["input_reference"], "quote": "USD"}
    result = summarize_journal([record], {"a": [ambiguous, foreign]}, 200000)
    assert all(row["counts"]["ambiguous"] == 1 for row in result["cohorts"])
    assert all(row["events"][0]["rejected_outcomes"][0]["reason"] == "outcome_lineage_mismatch"
               for row in result["cohorts"])
    result = summarize_journal([record], {"a": [ambiguous, definite]}, 200000)
    assert all(row["counts"]["resolved"] == 1 for row in result["cohorts"])


def test_early_definite_touch_is_resolved_but_endpoint_stays_pending():
    record = assessment()
    early = observed(record, clock=record["issued_at"] + 3600)
    result = summarize_journal([record], {"a": [early]}, early["observed_at"])
    assert event_cohort(result, "below_anchor_touch")["counts"]["resolved"] == 1
    assert event_cohort(result, "endpoint_below_anchor")["counts"]["pending"] == 1


def test_declared_bounds_report_omissions():
    records = [assessment(str(index)) for index in range(101)]
    outcomes = {"0": [observed(records[0], digest=str(index)) for index in range(257)]}
    result = summarize_journal(records, outcomes, 200000)
    assert result["assessment_count"] == 100
    assert result["truncation"] == {"assessments_omitted": 1, "outcomes_omitted": {"0": 1}}


@pytest.mark.parametrize("clock", [True, -1, "200000"])
def test_invalid_summary_clock_is_rejected(clock):
    with pytest.raises(ValueError, match="as_of"):
        summarize_journal([], {}, clock)
