"""Shared accumulation operations for headless CLI and read-only MCP adapters."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .evidence import archive_evidence, canonical_json, content_hash
from .plans import future_plan_snapshot


class AccumulationService:
    def __init__(self, settings: Settings, store, mode: str, capture, dataset_lineage=None):
        self.settings, self.store, self.mode, self.capture = settings, store, mode, capture
        self.dataset_lineage = dataset_lineage

    def context(self, asset="BTC", source=None, quote=None, as_of=None, lookbacks=None):
        from .context import market_context
        from .research import daily_bars

        captured = self.capture(asset, source, quote, as_of, profile="candles")
        result = market_context(list(captured.candles), captured.as_of, lookbacks)
        bars = daily_bars(list(captured.candles)).tail(731)
        history = [{"time": int(date.timestamp()), **{key: float(row[key]) for key in
                    ("open", "high", "low", "close", "volume")}} for date, row in bars.iterrows()]
        return {**result, "input_reference": captured.reference, "price_history": history,
                "input_truncation": captured.truncation}

    def purchase_windows(self, asset="BTC", source=None, quote=None, as_of=None, horizons=None, thresholds=None):
        from .purchase_windows import analyze_windows

        captured = self.capture(asset, source, quote, as_of, profile="candles")
        result = analyze_windows(list(captured.candles), captured.as_of, horizons=horizons,
                                 thresholds=thresholds, max_price_age=self.settings.market_interval * 2)
        return {**result, "input_reference": captured.reference, "input_truncation": captured.truncation}

    def _plan_inputs(self, config, as_of, *, captured=None):
        from .plans import validate_config

        config = validate_config(config)
        now = int(time.time()) if as_of is None else as_of
        if captured is None:
            rows = self.store.candles(config["asset"], config["source"], config["quote"], now,
                                      config["data_mode"], versions=True)
        else:
            if ((captured.asset, captured.source, captured.quote, captured.data_mode, captured.as_of)
                    != (config["asset"], config["source"], config["quote"], config["data_mode"], now)):
                raise ValueError("Plan inputs must match the captured market, mode and cutoff.")
            if captured.candle_versions is None:
                raise ValueError("Plan replay requires transaction-captured candle versions.")
            rows = list(captured.candle_versions)
        reference = {"source": config["source"], "quote": config["quote"], "asset": config["asset"],
                     "data_mode": config["data_mode"], "as_of": now, "configuration_hash": content_hash(config),
                     "content_hash": content_hash(rows),
                     "available_at": max((row.observed_at for row in rows), default=0)}
        if captured is not None:
            reference["watermark"] = dict(captured.watermark)
            if lineage := (captured.capture_profile or {}).get("dataset_lineage"):
                reference["dataset_lineage"] = json.loads(canonical_json(lineage))
        elif self.dataset_lineage:
            reference["dataset_lineage"] = self.dataset_lineage
        return config, now, rows, reference

    def compare_plans(self, config, as_of=None, validation="retrospective"):
        from .plans import compare_plans

        config, now, rows, reference = self._plan_inputs(config, as_of)
        return {**compare_plans(config, rows, now, validation=validation), "input_reference": reference}

    def accumulation_brief(self, asset="BTC", source=None, quote=None, as_of=None, plan_config=None):
        from .accumulation_brief import build_accumulation_brief
        from .plans import validate_config

        if plan_config is not None:
            config = validate_config(plan_config)
            if config["asset"] != asset or config["data_mode"] != self.mode:
                raise ValueError("The brief plan must match the requested asset and data mode.")
            if ((source is not None and source != config["source"])
                    or (quote is not None and quote != config["quote"])):
                raise ValueError("The brief plan must match its pinned source and native quote.")
            source, quote = config["source"], config["quote"]
        if not source or not quote:
            raise ValueError("An accumulation brief requires an explicit source and native quote, or a matching plan.")
        captured = self.capture(asset, source, quote, as_of, profile="candles")
        return build_accumulation_brief(captured, plan_config)

    def cost_sensitivity(self, config, cost_cases, as_of=None, validation="retrospective"):
        from .sensitivity import compare_cost_sensitivity

        config, now, rows, reference = self._plan_inputs(config, as_of)
        return {**compare_cost_sensitivity(config, rows, now, cost_cases, validation),
                "input_reference": reference}

    def journal_summary(self, as_of=None, limit=20):
        from .prospective import summarize_journal

        inputs = self._journal(read=True).summary_inputs(as_of, limit)
        return {**summarize_journal(inputs.records, inputs.outcomes, inputs.as_of),
                "selection": inputs.selection}

    def journal_scorecard(self, as_of=None, limit=20):
        from .scorecards import build_scorecard

        inputs = self._journal(read=True).summary_inputs(as_of, limit)
        return {**build_scorecard(inputs.records, inputs.outcomes, inputs.as_of),
                "selection": inputs.selection}

    def replay_assessment(self, assessment_id):
        from .recovery import replay_assessment

        if not isinstance(assessment_id, str) or not 1 <= len(assessment_id) <= 200:
            raise ValueError("assessment_id must contain 1–200 characters.")
        return replay_assessment(self._journal(read=True).get(assessment_id))

    def evaluate_plans(self, config, as_of=None, origins=None, validation="known_at",
                       held_out_start=None, policy_frozen_at=None):
        from .plan_evaluation import evaluate_plans

        config, now, rows, reference = self._plan_inputs(config, as_of)
        result = evaluate_plans(config, rows, now, origins, validation,
                                held_out_start=held_out_start, policy_frozen_at=policy_frozen_at)
        return {**result, "input_reference": reference}

    def _journal(self, *, read=False):
        from .journal import AssessmentJournal

        return AssessmentJournal.open_existing(self.store) if read else AssessmentJournal(self.store)

    def issue_assessment(self, idempotency_key, asset="BTC", source=None, quote=None,
                         as_of=None, plan_config=None, validation_protocol=None):
        from .context import market_context
        from .purchase_windows import analyze_windows

        issued = int(time.time())
        if as_of is not None and (type(as_of) is not int or as_of > issued):
            raise ValueError("An issued analysis cannot use a future as_of time.")
        request = {"asset": asset, "source": source, "quote": quote,
                   "as_of": as_of, "plan_config": plan_config, "data_mode": self.mode}
        if validation_protocol is not None:
            from .validation_protocol import validate_validation_protocol

            validation_protocol = validate_validation_protocol(validation_protocol)
            if validation_protocol["frozen_at"] > issued:
                raise ValueError("The validation protocol cannot be frozen after actual issuance.")
            request["validation_protocol_hash"] = validation_protocol["protocol_hash"]
        request_hash = content_hash(request)
        journal = self._journal()
        # Return the original on a retry, even if collection or wall time changed.
        original = journal.original_for_request(idempotency_key, request_hash)
        if original is not None:
            return original
        if plan_config is not None:
            from .plans import validate_config
            plan_identity = validate_config(plan_config)
            if plan_identity["asset"] != asset or plan_identity["data_mode"] != self.mode:
                raise ValueError("An issued plan must match the assessment asset and data mode.")
            if ((source is not None and source != plan_identity["source"])
                    or (quote is not None and quote != plan_identity["quote"])):
                raise ValueError("An issued plan must match the assessment source and native quote.")
            source, quote = plan_identity["source"], plan_identity["quote"]
        captured = self.capture(asset, source, quote, issued if as_of is None else as_of, profile="candles",
                                include_candle_versions=plan_config is not None)
        context = market_context(list(captured.candles), captured.as_of)
        window_parameters = {}
        if validation_protocol is not None:
            declaration = validation_protocol["declaration"]
            cohorts = [row for row in declaration["cohorts"] if all(
                row[key] == captured.reference.get(key) for key in ("asset", "source", "quote", "data_mode", "dataset_lineage"))]
            if not cohorts:
                raise ValueError("No declared validation cohort matches the captured native market and lineage.")
            if declaration["mode"] == "prospective" and not declaration["starts_at"] <= issued < declaration["ends_at"]:
                raise ValueError("Actual issuance is outside the declared prospective period.")
            horizons = sorted({event["horizon_days"] for row in cohorts for event in row["events"]})
            thresholds = sorted({event["threshold"] for row in cohorts for event in row["events"]
                                 if event["threshold"] is not None})
            if len(horizons) > 16 or len(thresholds) > 12 or len(horizons) * max(1, len(thresholds)) > 64:
                raise ValueError("The selected validation cohort exceeds its 64-cell issuance window budget.")
            window_parameters = {"horizons": horizons, "thresholds": thresholds or [0.05]}
        windows = analyze_windows(list(captured.candles), captured.as_of, issued_at=issued,
                                  max_price_age=self.settings.market_interval * 2, **window_parameters)
        payload = {"kind": "accumulation_assessment", "request": request, "request_hash": request_hash,
                   "input_reference": captured.reference, "config": captured.configuration,
                   "rules": {"context": 1, "purchase_windows": 1, "outcome_protocol": 1}, "context": context,
                   "outcome_protocol": {"schema_version": 1, "endpoint_convention": "hourly_after_deadline_v1",
                                        "future_price_selection": "first_observed_version",
                                        "forecast_comparability": "not_established_for_anchor_relative_estimates"},
                   "windows": windows, "plan": None,
                   "historical_calculation": captured.as_of != issued}
        if validation_protocol is not None:
            from .prospective_validation import build_validation_predictions

            payload["validation_protocol_hash"] = validation_protocol["protocol_hash"]
            payload["validation_predictions"] = build_validation_predictions(
                validation_protocol, captured.reference, windows, payload["rules"], issued)
        evidence = captured.evidence()
        if plan_config is not None:
            config, now, rows, reference = self._plan_inputs(plan_config, captured.as_of, captured=captured)
            from .plans import compare_plans
            result = (compare_plans(config, rows, now) if config["start_time"] <= now
                      else future_plan_snapshot(config, now))
            payload["plan"] = {**result, "input_reference": reference}
            consumed = set(captured.candles)
            evidence["plan_candle_versions"] = [asdict(row) for row in rows if row not in consumed]
            evidence["plan_uses_captured_candles"] = True
        evidence = archive_evidence(evidence)
        payload["evidence_budget"] = {"uncompressed_bytes": evidence["uncompressed_bytes"],
                                      "maximum_uncompressed_bytes": 32 * 1024 * 1024,
                                      "capture_profile": "candles"}
        try:
            return journal.issue(idempotency_key, payload, evidence, issued)
        except ValueError:
            original = journal.original_for_request(idempotency_key, request_hash)
            if original is not None:
                return original
            raise

    def assessment(self, assessment_id):
        return self._journal(read=True).get(assessment_id)

    def assessment_outcomes(self, assessment_id):
        return self._journal(read=True).outcomes(assessment_id)

    def observe_assessment(self, assessment_id, as_of=None):
        from .purchase_windows import resolve_window

        now = int(time.time()) if as_of is None else as_of
        if type(now) is not int or now > int(time.time()):
            raise ValueError("Outcome observations cannot be dated in the future.")
        journal = self._journal(read=True)
        original = journal.get(assessment_id)
        payload = original["payload"]
        if payload.get("kind") != "accumulation_assessment":
            raise ValueError("This journal record is not an accumulation assessment.")
        reference = payload["input_reference"]
        protocol = payload.get("outcome_protocol")
        if protocol and (protocol.get("schema_version") != 1
                         or protocol.get("endpoint_convention") != "hourly_after_deadline_v1"):
            raise ValueError("The frozen outcome protocol is unsupported; original records remain unchanged.")
        rows = self.store.candles(reference["asset"], reference["source"], reference["quote"], now,
                                  reference["data_mode"], versions=bool(protocol))
        if protocol:
            first_versions = {}
            for row in rows:
                first_versions.setdefault(row.open_time, row)
            rows = list(first_versions.values())
        windows = payload["windows"]
        anchor = windows.get("anchor") or {}
        results = []
        anchor_price = anchor.get("price") or windows.get("anchor_price")
        anchor_time = anchor.get("time") or windows.get("anchor_time")
        if isinstance(anchor_time, str):
            from datetime import datetime
            anchor_time = int(datetime.fromisoformat(anchor_time).timestamp())
        if anchor_price is not None and anchor_time is not None:
            for horizon, thresholds in windows.get("windows", {}).items():
                for threshold in thresholds:
                    results.append(resolve_window(rows, anchor_price, anchor_time, original["issued_at"],
                                                  int(horizon), float(threshold), now,
                                                  **({"endpoint_convention": protocol["endpoint_convention"]} if protocol else {})))
        outcome = {"as_of": now, "original_input_reference": reference,
                   "input_reference": {**reference, "as_of": now,
                                       "available_at": max((row.observed_at for row in rows), default=0),
                                       "content_hash": content_hash(rows)},
                   "windows": results, "status": "observed" if results else "insufficient"}
        frozen_plan = payload.get("plan")
        if frozen_plan is not None:
            from .evidence import restore_evidence
            from .models import Candle
            from .plans import RULE_VERSION, compare_plans

            config = frozen_plan["config"]
            validation = frozen_plan.get("validation_mode", "retrospective")
            if frozen_plan.get("rule_version") != RULE_VERSION:
                outcome["plan"] = {"status": "unsupported", "reason": "frozen_plan_rule_version_unavailable",
                                   "rule_version": frozen_plan.get("rule_version"), "config": config}
            else:
                evidence = restore_evidence(original["evidence"])
                identity = (config["source"], config["asset"], config["quote"], config["data_mode"])

                def matches(row):
                    return (row.source, row.asset, row.quote, row.data_mode) == identity

                # Only the original consumed vintages may describe pre-issuance
                # decisions. Store retention or subsequent revisions cannot replace them.
                pinned = {}
                for item in evidence.get("candles", []) + evidence.get("plan_candle_versions", []):
                    row = Candle(**item)
                    if matches(row):
                        pinned[content_hash(item)] = row
                future_rows = self.store.candles(config["asset"], config["source"], config["quote"], now,
                                                 config["data_mode"], versions=True)
                future = {}
                excluded_boundary = 0
                for row in future_rows:
                    if row.open_time < original["issued_at"]:
                        excluded_boundary += row.close_time > original["issued_at"]
                        continue
                    # First observed future vintages remain stable across repeated
                    # observations; a later revision cannot rewrite a simulated step.
                    key = (row.timeframe, row.open_time)
                    prior = future.get(key)
                    if prior is None or row.observed_at < prior.observed_at:
                        future[key] = row
                frozen_rows = sorted(pinned.values(), key=lambda row: (row.open_time, row.observed_at))
                added_rows = sorted(future.values(), key=lambda row: (row.open_time, row.observed_at))
                merged = frozen_rows + added_rows
                result = (compare_plans(config, merged, now, validation=validation)
                          if config["start_time"] <= now else future_plan_snapshot(config, now))
                result["input_reference"] = {
                    "as_of": now, "source": config["source"], "asset": config["asset"],
                    "quote": config["quote"], "data_mode": config["data_mode"],
                    "available_at": max((row.observed_at for row in merged), default=0),
                    **({"dataset_lineage": reference["dataset_lineage"]} if "dataset_lineage" in reference else {}),
                    "configuration_hash": content_hash(config), "content_hash": content_hash(merged),
                }
                result["replay"] = {
                    "assessment_id": assessment_id, "original_content_hash": original["content_hash"],
                    "original_input_reference": frozen_plan.get("input_reference"),
                    "frozen_rule_version": frozen_plan["rule_version"],
                    "pinned_candle_versions": len(frozen_rows), "pinned_content_hash": content_hash(frozen_rows),
                    "future_candle_versions": len(added_rows), "future_content_hash": content_hash(added_rows),
                    "future_price_selection": "first_observed_version_fully_post_issuance",
                    "boundary_policy": "exclude_bars_straddling_actual_issuance",
                    "excluded_straddling_versions": excluded_boundary,
                }
                outcome["plan"] = result
                if not results:
                    complete = any(terminal["status"] == "complete" for plan in result["plans"].values()
                                   for terminal in plan["terminals"].values())
                    outcome["status"] = "observed" if complete else "pending"
        key = "observed-" + content_hash(outcome)
        return journal.append_outcome(assessment_id, key, outcome, now)

    def vigilance(self, asset="BTC", source=None, quote=None, as_of=None, horizon_hours=4, policy=None):
        from .vigilance import assess_vigilance

        captured = self.capture(asset, source, quote, as_of, profile="vigilance", vigilance_policy=policy)
        rules = {"price_max_age": min(7200, self.settings.market_interval * 2),
                 "liquidity_max_age": min(3600, self.settings.liquidity_interval * 2),
                 "derivatives_max_age": min(86400, self.settings.market_interval * 2), **(policy or {})}
        result = assess_vigilance(list(captured.candles), list(captured.observations),
                                  captured.as_of, horizon_hours, rules)
        return {**result, "input_reference": captured.reference}

    def record_vigilance(self, state_key, asset="BTC", source=None, quote=None,
                         as_of=None, horizon_hours=4, policy=None):
        from .vigilance import transition_vigilance

        if not isinstance(state_key, str) or not 1 <= len(state_key) <= 128:
            raise ValueError("state_key must contain 1 to 128 characters.")
        assessment = self.vigilance(asset, source, quote, as_of, horizon_hours, policy)
        now = int(time.time())
        with self.store.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS vigilance_states (state_key TEXT PRIMARY KEY, record TEXT NOT NULL)")
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT record FROM vigilance_states WHERE state_key=?", (state_key,)).fetchone()
            state = transition_vigilance(json.loads(prior[0]) if prior else None, assessment, now)
            con.execute("INSERT INTO vigilance_states VALUES(?,?) ON CONFLICT(state_key) DO UPDATE SET record=excluded.record",
                        (state_key, canonical_json(state)))
        if state.get("alert"):
            self._journal().issue("watch-" + content_hash([state_key, state["evidence_fingerprint"]]),
                                  {"kind": "vigilance", "state_key": state_key, "alert": state["alert"],
                                   "rules": state["policy"]}, assessment.get("evidence", {}), now)
        return state

    def import_history(self, path, source, asset, quote, member=None, expected_sha256=None):
        from .history_import import import_history

        result = import_history(path, source=source, asset=asset, quote=quote, observed_at=int(time.time()),
                                member=member, expected_sha256=expected_sha256)
        inserted = self.store.put_candles(result.candles)
        self.store.save_local_record("history_import", result.provenance)
        return {"inserted": inserted, "data_mode": "import", "provenance": result.provenance}

    def import_calendar(self, path, kind="bls"):
        from .calendars import parse_bls_ics, parse_ecb_manual_schedule, parse_fomc_calendar

        path = Path(path)
        if kind == "fed":
            kind = "fomc"
        if path.stat().st_size > 2_000_000 or kind not in ("bls", "fomc", "ecb-manual"):
            raise ValueError("Use a BLS/FOMC calendar or explicit ECB manual schedule of at most 2 MB.")
        parser = {"bls": parse_bls_ics, "fomc": parse_fomc_calendar,
                  "ecb-manual": parse_ecb_manual_schedule}[kind]
        events = parser(path.read_text(encoding="utf-8"), int(time.time()))
        inserted = sum(self.store.save_local_record("calendar", event)["inserted"] for event in events)
        return {"inserted": inserted, "events": events, "origin": "calendar_import"}

    def collect_calendar(self):
        from .calendars import collect_calendars
        from .providers import PublicHTTP

        client = PublicHTTP()
        try:
            result = collect_calendars(int(time.time()), client=client, receipt_clock=time.time)
        finally:
            client.close()
        for event in result["events"]:
            self.store.save_local_record("calendar", event)
        return result
