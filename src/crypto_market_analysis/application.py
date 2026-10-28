"""Shared accumulation operations for headless CLI and read-only MCP adapters."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .evidence import archive_evidence, canonical_json, content_hash


def _future_plan(config, as_of):
    from .plans import RULE_VERSION

    return {"schema_version": 1, "rule_version": RULE_VERSION, "as_of": as_of,
            "status": "not_started", "config": config, "validation_mode": "retrospective",
            **{key: config[key] for key in ("source", "asset", "quote", "data_mode")},
            "plans": {family: {"status": "not_started", "terminals": {
                str(days): {"status": "pending", "time": config["start_time"] + days * 86400,
                            "reason": "future_horizon"} for days in (90, 180)}}
                      for family in ("regular", "split", "deferred")},
            "assumptions": ["The frozen plan has not started; no future price, fill or outcome is assumed."]}


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

    def _plan_inputs(self, config, as_of):
        from .plans import validate_config

        config = validate_config(config)
        now = int(time.time()) if as_of is None else as_of
        rows = self.store.candles(config["asset"], config["source"], config["quote"], now,
                                  config["data_mode"], versions=True)
        reference = {"source": config["source"], "quote": config["quote"], "asset": config["asset"],
                     "data_mode": config["data_mode"], "as_of": now, "configuration_hash": content_hash(config),
                     "content_hash": content_hash(rows),
                     "available_at": max((row.observed_at for row in rows), default=0)}
        if self.dataset_lineage:
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

        now = int(time.time()) if as_of is None else as_of
        if type(now) is not int or now < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Use a nonnegative UTC cutoff and a journal limit from 1 through 100.")
        with self.store.connection() as con:
            con.execute("BEGIN")
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_journal'").fetchone()
            total = con.execute("SELECT COUNT(*) FROM assessment_journal WHERE issued_at<=?", (now,)).fetchone()[0] if exists else 0
            rows = con.execute("SELECT json_remove(record,'$.evidence') FROM assessment_journal WHERE issued_at<=? ORDER BY issued_at DESC,assessment_id LIMIT ?",
                               (now, limit)).fetchall() if exists else []
            records = [json.loads(row[0]) for row in rows]
            outcomes, truncated = {}, []
            for record in records:
                identity = record["assessment_id"]
                values = con.execute("SELECT record FROM assessment_outcomes WHERE assessment_id=? AND json_extract(record,'$.observed_at')<=? ORDER BY sequence LIMIT 257",
                                     (identity, now)).fetchall()
                outcomes[identity] = [json.loads(row[0]) for row in values[:256]]
                if len(values) > 256:
                    truncated.append(identity)
        return {**summarize_journal(records, outcomes, now),
                "selection": {"scope": "most_recent_issuances_at_or_before_cutoff", "limit": limit,
                              "available_records": total, "records_truncated": total > limit,
                              "outcome_limit_per_record": 256, "outcomes_truncated": truncated}}

    def evaluate_plans(self, config, as_of=None, origins=None, validation="known_at",
                       held_out_start=None, policy_frozen_at=None):
        from .plan_evaluation import evaluate_plans

        config, now, rows, reference = self._plan_inputs(config, as_of)
        result = evaluate_plans(config, rows, now, origins, validation,
                                held_out_start=held_out_start, policy_frozen_at=policy_frozen_at)
        return {**result, "input_reference": reference}

    def _journal(self, *, read=False):
        from .journal import AssessmentJournal

        if read:
            with self.store.connection() as con:
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_journal'").fetchone() is None:
                    raise KeyError("Assessment not found; no analyses have been issued.")
        return AssessmentJournal(self.store)

    def issue_assessment(self, idempotency_key, asset="BTC", source=None, quote=None,
                         as_of=None, plan_config=None):
        from .context import market_context
        from .purchase_windows import analyze_windows

        issued = int(time.time())
        if as_of is not None and (type(as_of) is not int or as_of > issued):
            raise ValueError("An issued analysis cannot use a future as_of time.")
        request = {"asset": asset, "source": source, "quote": quote,
                   "as_of": as_of, "plan_config": plan_config, "data_mode": self.mode}
        request_hash = content_hash(request)
        journal = self._journal()
        # Return the original on a retry, even if collection or wall time changed.
        with self.store.connection() as con:
            prior = con.execute("SELECT record FROM assessment_journal WHERE idempotency_key=?",
                                (idempotency_key,)).fetchone()
        if prior:
            record = json.loads(prior[0])
            if record["payload"].get("request_hash") != request_hash:
                raise ValueError("Idempotency key already belongs to a different request.")
            return record
        if plan_config is not None:
            from .plans import validate_config
            plan_identity = validate_config(plan_config)
            if plan_identity["asset"] != asset or plan_identity["data_mode"] != self.mode:
                raise ValueError("An issued plan must match the assessment asset and data mode.")
            if ((source is not None and source != plan_identity["source"])
                    or (quote is not None and quote != plan_identity["quote"])):
                raise ValueError("An issued plan must match the assessment source and native quote.")
            source, quote = plan_identity["source"], plan_identity["quote"]
        captured = self.capture(asset, source, quote, issued if as_of is None else as_of, profile="candles")
        context = market_context(list(captured.candles), captured.as_of)
        windows = analyze_windows(list(captured.candles), captured.as_of, issued_at=issued,
                                  max_price_age=self.settings.market_interval * 2)
        payload = {"kind": "accumulation_assessment", "request": request, "request_hash": request_hash,
                   "input_reference": captured.reference, "config": captured.configuration,
                   "rules": {"context": 1, "purchase_windows": 1, "outcome_protocol": 1}, "context": context,
                   "outcome_protocol": {"schema_version": 1, "endpoint_convention": "hourly_after_deadline_v1",
                                        "future_price_selection": "first_observed_version",
                                        "forecast_comparability": "not_established_for_anchor_relative_estimates"},
                   "windows": windows, "plan": None,
                   "historical_calculation": captured.as_of != issued}
        evidence = captured.evidence()
        if plan_config is not None:
            config, now, rows, reference = self._plan_inputs(plan_config, captured.as_of)
            from .plans import compare_plans
            result = compare_plans(config, rows, now) if config["start_time"] <= now else _future_plan(config, now)
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
            with self.store.connection() as con:
                prior = con.execute("SELECT record FROM assessment_journal WHERE idempotency_key=?",
                                    (idempotency_key,)).fetchone()
            if prior:
                original = json.loads(prior[0])
                if original["payload"].get("request_hash") == request_hash:
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
                          if config["start_time"] <= now else _future_plan(config, now))
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
        from .calendars import parse_bls_ics, parse_fomc_calendar

        path = Path(path)
        if kind == "fed":
            kind = "fomc"
        if path.stat().st_size > 2_000_000 or kind not in ("bls", "fomc"):
            raise ValueError("Use a BLS/FOMC calendar file of at most 2 MB.")
        parser = parse_bls_ics if kind == "bls" else parse_fomc_calendar
        events = parser(path.read_text(encoding="utf-8"), int(time.time()))
        inserted = sum(self.store.save_local_record("calendar", event)["inserted"] for event in events)
        return {"inserted": inserted, "events": events, "origin": "calendar_import"}

    def collect_calendar(self):
        from .calendars import collect_calendars
        from .providers import PublicHTTP

        client = PublicHTTP()
        try:
            result = collect_calendars(int(time.time()), client=client)
        finally:
            client.close()
        for event in result["events"]:
            self.store.save_local_record("calendar", event)
        return result
