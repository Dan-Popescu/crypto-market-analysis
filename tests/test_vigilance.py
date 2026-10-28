"""Source-native stress fixtures and restart-safe lifecycle checks."""

import copy
import json
from dataclasses import replace

import pytest

from crypto_market_analysis.models import Candle, Observation
from crypto_market_analysis.vigilance import assess_vigilance, transition_vigilance

NOW = 100 * 86400


def candles(now=NOW, falling=True):
    now = now // 3600 * 3600
    return [Candle("venue", "BTC", "EUR", "1h", now - 7200, now - 3600, 100, 100, 100, 100, 10, now - 3600),
            Candle("venue", "BTC", "EUR", "1h", now - 3600, now, 100, 100, 97 if falling else 100,
                   97 if falling else 100, 10, now)]


def observations(now=NOW, stress=True):
    rows = []
    for metric, unit, baseline, current in (
        ("spread_bps", "bps", 2, 10 if stress else 2),
        ("depth_bid_1pct", "EUR", 1000, 400 if stress else 1000),
        ("depth_ask_1pct", "EUR", 1000, 400 if stress else 1000),
    ):
        for offset in range(-3600, 1, 300):
            rows.append(Observation("venue", "BTC", metric, now + offset, now + offset,
                                    current if offset == 0 else baseline, unit, "EUR",
                                    {"coverage_complete": True, "timestamp_source": "exchange", "symbol": "BTC/EUR"}))
    return rows


def assessment(now=NOW, stress=True):
    return assess_vigilance(candles(now, stress), observations(now, stress), now)


def test_stress_uses_hand_calculated_source_native_baseline():
    result = assessment()
    assert result["state"] == "elevated"
    assert result["quality"] == "sufficient"
    assert result["price"]["change_pct"] == pytest.approx(-3)
    assert result["liquidity"]["spread_bps"]["ratio_to_baseline"] == 5
    assert result["liquidity"]["depth_bid_1pct"]["ratio_to_baseline"] == .4
    assert result["expires_at"] == NOW + 600
    assert len(result["reasons"]) == 4
    assert result["market"] == {"source": "venue", "asset": "BTC", "quote": "EUR", "data_mode": "live"}
    assert result["issued_at"] is None
    json.dumps(result, allow_nan=False)


def test_missing_stale_unknown_event_and_retention_aggregate_do_not_look_sufficient():
    assert assess_vigilance([], [], NOW)["quality"] == "insufficient"
    stale = assess_vigilance(candles(), observations(), NOW + 600)
    assert stale["quality"] == "degraded"
    assert stale["expires_at"] == NOW + 600
    fallback = [replace(o, metadata={**o.metadata, "timestamp_source": "observation"}) for o in observations()]
    assert assess_vigilance(candles(), fallback, NOW)["quality"] == "degraded"
    aggregates = [replace(o, metadata={**o.metadata, "aggregation": "hourly_median"}) for o in observations()]
    assert assess_vigilance(candles(), aggregates, NOW)["quality"] == "degraded"
    partial = [replace(o, metadata={**o.metadata, "coverage_complete": False}) for o in observations()]
    assert assess_vigilance(candles(), partial, NOW)["quality"] == "degraded"
    transition = transition_vigilance(None, stale, NOW + 600)
    assert transition["alert"] is None
    assert transition["state"] == "expired"


def test_price_stale_reingestion_does_not_refresh_event_age():
    rows = [replace(c, observed_at=NOW + 7200) for c in candles()]
    result = assess_vigilance(rows, observations(NOW + 7200), NOW + 7200)
    assert result["quality"] == "insufficient"
    assert result["price"]["status"] == "stale"


def test_coverage_counts_bins_and_refuses_mixed_dimensions():
    # Many snapshots within one five-minute bin cannot replace an hour of history.
    burst = [replace(o, event_time=NOW - (index + 1), observed_at=NOW - (index + 1))
             for index, o in enumerate(observations())]
    assert assess_vigilance(candles(), burst, NOW)["quality"] == "degraded"
    mixed = [*observations(), replace(observations()[-1], metadata={"symbol": "OTHER", "coverage_complete": True})]
    assert assess_vigilance(candles(), mixed, NOW)["liquidity"]["depth_ask_1pct"]["status"] == "ambiguous_identity"
    wrong_quote = [replace(o, quote="USD", unit="USD" if o.unit == "EUR" else o.unit) for o in observations()]
    assert assess_vigilance(candles(), wrong_quote, NOW)["quality"] == "degraded"
    with pytest.raises(ValueError, match="Select one"):
        assess_vigilance([*candles(), replace(candles()[-1], quote="USD")], [], NOW)


def test_optional_derivatives_keep_period_source_and_native_units_separate():
    rows = observations()
    for source, unit, period, value in (("swap_a", "rate", 8, .001), ("swap_b", "rate", 1, .02)):
        rows.append(Observation(source, "BTC", "funding_rate_actual", NOW, NOW, value, unit, "EUR",
                                {"symbol": "BTC/EUR:EUR", "funding_interval_hours": period}))
    rows.append(Observation("swap_a", "BTC", "open_interest_amount", NOW, NOW, 999, "unknown", "EUR"))
    result = assess_vigilance(candles(), rows, NOW)
    assert result["quality"] == "sufficient"
    assert len(result["derivatives"]) == 3
    assert {d["current"]["metadata"]["funding_interval_hours"] for d in result["derivatives"]
            if d["current"]["metric"].startswith("funding")} == {1, 8}
    assert next(d for d in result["derivatives"] if d["current"]["metric"].startswith("open_interest"))["status"] == "unknown_native_convention"


def test_polling_identical_evidence_never_reissues_or_renews_expiry_after_restart():
    result = assessment()
    state = transition_vigilance(None, result, NOW)
    assert state["alert"]["kind"] == "elevated_vigilance"
    assert state["records_used"] == 1
    frozen = copy.deepcopy(state)
    restart = json.loads(json.dumps(state))
    later = assess_vigilance(candles(), observations(), NOW + 300)
    assert later["evidence_fingerprint"] == result["evidence_fingerprint"]
    duplicate = transition_vigilance(restart, later, NOW + 300)
    assert duplicate["alert"] is None
    assert duplicate["assessment_expires_at"] == frozen["assessment_expires_at"]
    assert duplicate["records_used"] == 1
    expired = transition_vigilance(duplicate, later, NOW + 600)
    assert expired["state"] == "expired"
    assert expired["alert"] is None
    assert state == frozen


def test_recovery_hysteresis_and_cooldown():
    state = transition_vigilance(None, assessment(), NOW)
    state = transition_vigilance(state, assessment(NOW + 300, False), NOW + 300)
    assert state["state"] == "elevated"
    assert state["transition_reason"] == "recovery_pending"
    state = transition_vigilance(state, assessment(NOW + 600, False), NOW + 600)
    assert state["state"] == "normal"
    assert state["transition_reason"] == "recovered"
    state = transition_vigilance(state, assessment(NOW + 900), NOW + 900)
    assert state["state"] == "elevated"
    assert state["alert"] is None
    assert state["transition_reason"] == "cooldown"
    # A later alert requires a new transition after recovery and cooldown.
    for offset in (1200, 1500):
        state = transition_vigilance(state, assessment(NOW + offset, False), NOW + offset)
    state = transition_vigilance(state, assessment(NOW + 3900), NOW + 3900)
    assert state["alert"] is not None


def test_recovery_cannot_use_missing_or_duplicate_inputs():
    state = transition_vigilance(None, assessment(), NOW)
    normal = assessment(NOW + 300, False)
    state = transition_vigilance(state, normal, NOW + 300)
    state = transition_vigilance(state, normal, NOW + 400)
    assert state["recovery_count"] == 1
    degraded = assess_vigilance(candles(NOW + 600, False), [], NOW + 600)
    state = transition_vigilance(state, degraded, NOW + 600)
    assert state["recovery_count"] == 0
    assert state["state"] == "expired"
    assert state["alert"] is None


def test_fixed_watch_end_frequency_storage_and_future_time_validation():
    policy = {"watch_seconds": 3600, "max_records": 13}
    first = assess_vigilance(candles(), observations(), NOW, policy=policy)
    state = transition_vigilance(None, first, NOW)
    fresh = assess_vigilance(candles(NOW + 60), observations(NOW + 60), NOW + 60, policy=policy)
    skipped = transition_vigilance(state, fresh, NOW + 60)
    assert skipped["records_used"] == 1
    assert skipped["transition_reason"] == "poll_interval_not_elapsed"
    expired = transition_vigilance(skipped, first, NOW + 3600)
    assert expired["state"] == "expired"
    assert expired["watch_expires_at"] == NOW + 3600
    with pytest.raises(ValueError, match="future"):
        transition_vigilance(None, assessment(), NOW - 1)
    with pytest.raises(ValueError, match="backwards"):
        transition_vigilance(skipped, first, NOW + 30)
    for bad_policy in ({"watch_seconds": 999999}, {"poll_seconds": 1}, {"max_records": 2},
                       {"price_drop_pct": float("nan")}, {"minimum_samples": True}, {"unknown": 1}):
        with pytest.raises(ValueError):
            assess_vigilance(candles(), observations(), NOW, policy=bad_policy)


def test_future_and_revision_isolation_and_finite_output():
    original = assessment()
    future = [replace(o, value=1, observed_at=NOW + 1) for o in observations()]
    assert assess_vigilance(candles(), [*observations(), *future], NOW) == original
    reingested = [replace(o, observed_at=NOW + 1) for o in observations()]
    assert assess_vigilance(candles(), reingested, NOW + 1)["evidence_fingerprint"] == original["evidence_fingerprint"]
    with pytest.raises(ValueError):
        assess_vigilance(candles(), [replace(observations()[0], value=float("inf"))], NOW)
    with pytest.raises(ValueError):
        assess_vigilance(candles(), observations(), True)
    with pytest.raises(ValueError):
        assess_vigilance(candles(), observations(), NOW, horizon_hours=1)


def test_watch_cannot_collapse_quote_mode_or_horizon_identity():
    first = assessment()
    state = transition_vigilance(None, first, NOW)
    other = copy.deepcopy(first)
    other["market"]["quote"] = "USD"
    with pytest.raises(ValueError, match="identity"):
        transition_vigilance(state, other, NOW + 300)
    other = assess_vigilance(candles(), observations(), NOW, horizon_hours=8)
    with pytest.raises(ValueError, match="horizon"):
        transition_vigilance(state, other, NOW + 300)
