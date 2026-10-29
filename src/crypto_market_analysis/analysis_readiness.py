"""Evidence-backed scope and actionable omissions from one read-only snapshot.

Readiness describes which claims the retained evidence supports. It is not a
financial score, a source licence, or French purchase authorization.
"""

from __future__ import annotations

import json
import shlex
from copy import deepcopy
from datetime import datetime

from .analysis_dossier import MAX_RESULT_BYTES, _validate, build_analysis_dossier
from .evidence import canonical_json, content_hash
from .models import Candle
from .purchase_windows import analyze_windows
from .qualification import _selection, qualify_sources
from .reporting import bounded_as_of, bounded_object
from .storage import Store

_QUALIFICATION_FIELDS = {
    "required_history_days", "required_start", "required_end", "freshness_seconds", "max_gap_hours",
    "max_gap_details", "max_evidence_rows", "max_query_rows", "max_requests", "max_storage_bytes",
}


def _request(request, cutoff):
    bounded_object(request, "analysis readiness request")
    if len(canonical_json(request).encode()) > 65536:
        raise ValueError("Analysis readiness request exceeds the 64 KiB limit.")
    if set(request) - {"dossier", "windows", "qualification", "freshness_seconds"} or "dossier" not in request:
        raise ValueError("Readiness requires dossier and only optional windows, qualification and freshness_seconds.")
    request = deepcopy(request)
    request["dossier"] = _validate(request["dossier"])
    freshness = request.get("freshness_seconds", 7200)
    if type(freshness) is not int or not 0 <= freshness <= 604800:
        raise ValueError("freshness_seconds must be an integer from zero to 604800.")
    request["freshness_seconds"] = freshness
    options = request.get("qualification", {})
    if not isinstance(options, dict) or set(options) - _QUALIFICATION_FIELDS:
        raise ValueError("Readiness qualification accepts coverage/resource bounds only; selectors derive from dossier.")
    selected = request["dossier"]["markets"]
    markets, sources = [], {}
    for row in selected:
        market = row["asset"] + "/" + row["quote"]
        if market not in markets:
            markets.append(market)
        sources.setdefault(market, []).append(row["source"])
    end = options.get("required_end", cutoff // 3600 * 3600)
    days = options.get("required_history_days", 730)
    qualification = {"data_mode": request["dossier"]["data_mode"], "markets": markets,
                     "market_sources": sources, "freshness_seconds": freshness, **options}
    if "required_start" not in options and type(end) is int and type(days) is int:
        qualification["required_start"] = max(0, end - days * 86400)
    _selection(qualification, cutoff)
    if "windows" in request:
        windows = request["windows"]
        if not isinstance(windows, dict) or set(windows) - {"horizons", "thresholds", "max_price_age"}:
            raise ValueError("Readiness windows accepts horizons, thresholds and max_price_age only.")
        analyze_windows([], cutoff, **windows)
    return request, qualification


def _status(value):
    if value in ("available", "ok", "pass", "fresh", "experimental", "hypothetical", "diagnostic", "supported", "success"):
        return "available"
    if value == "not_requested":
        return value
    if value in ("missing", "unavailable", "missing_endpoint"):
        return "missing"
    if "conflict" in str(value):
        return "contradictory"
    if "stale" in str(value) or value == "expired":
        return "stale"
    if value in ("insufficient", "partial", "unknown", "pending", "ambiguous", "untested"):
        return "incomplete"
    return "unsupported"


def _claim(claim_id, result, refs, *, reasons=None, needed=None, demo=False, details=None):
    calculation = result.get("status", "missing")
    status = _status(calculation)
    if result.get("coverage", {}).get("calculation_truncated"):
        status = "unsupported"
    fallback_reasons = [] if status == "available" else [str(calculation)]
    return {"claim": claim_id, "status": "demo_only" if demo and status == "available" else status,
            "calculation_status": calculation,
            "reasons": list(reasons if reasons is not None else result.get("reasons", []))
                       or ([result["reason"]] if result.get("reason") else fallback_reasons),
            "evidence_references": refs, "needed_inputs": list(needed or []),
            "details": deepcopy(result if details is None else details)}


def _refs(section):
    evidence = section.get("stored_inputs", {})
    return ["sha256:" + evidence["input_hash"]] if evidence.get("input_hash") else []


def _market_claims(market, empirical, windows, demo, cutoff, freshness):
    claims = []
    context = market["market_context"]
    refs = _refs(context)
    anchor = context.get("anchor")
    if anchor:
        event = int(datetime.fromisoformat(anchor["time"]).timestamp())
        receipt = int(datetime.fromisoformat(anchor["observed_at"]).timestamp())
        fresh = cutoff - event <= freshness and cutoff - receipt <= freshness
        claims.append(_claim("hourly_anchor", {"status": "available" if fresh else "stale", **anchor,
                             "event_age_seconds": cutoff - event, "receipt_age_seconds": cutoff - receipt,
                             "maximum_age_seconds": freshness, "freshness_boundary": "both ages <= maximum"}, refs,
                             reasons=[] if fresh else ["anchor_event_or_receipt_exceeds_maximum_age"],
                             needed=[] if fresh else ["Fresh completed native hourly close and receipt"], demo=demo))
    else:
        claims.append(_claim("hourly_anchor", {"status": "missing"}, refs,
                             reasons=["no_known_completed_native_hour"], needed=["Native hourly candles"]))
    for length, lookback in context["lookbacks"].items():
        for metric, result in lookback.items():
            if not isinstance(result, dict) or "coverage" not in result:
                continue
            needed = [] if result["status"] == "available" else [
                f"Complete required UTC days: {result['coverage']['start']} to {result['coverage']['end']}"]
            claims.append(_claim(f"context.{length}d.{metric}", result, refs, needed=needed, demo=demo))
    for period, result in context["calendar"].items():
        for metric in ("return", "volume", "previous_period_volume_ratio"):
            claims.append(_claim(f"calendar_context.{period}.{metric}", result[metric], refs,
                                 needed=[] if result[metric]["status"] == "available" else
                                 ["Complete current and, for ratios, preceding completed UTC calendar period"], demo=demo))
    claims.append(_claim("empirical_hourly_history", empirical, [empirical["evidence"]["reference"]],
                         reasons=empirical["reasons"], needed=[] if empirical["status"] == "pass" else
                         ["Known-at aligned native hours covering the explicit qualification range"], demo=demo,
                         details={key: empirical[key] for key in ("coverage", "freshness", "source_access", "source_health")}))
    claims.append(_claim("source_runtime_access", empirical["source_access"], [empirical["evidence"]["reference"]],
                         reasons=["Stored candles or source-wide runs do not prove current native-market API access"],
                         needed=["Explicit public native-market capture evidence; inspect latest source-wide failures"],
                         details={"access": empirical["source_access"], "source_health": empirical["source_health"]}))
    # Permission acceptance is deliberately independent of all numerical claims.
    claims.append(_claim("data_reuse_permission", {"status": "unsupported"},
                         ["sha256:" + content_hash(empirical["rights"])],
                         reasons=["Intended-use permission is not established by public data access"],
                         needed=["Review and accept source rights for the intended use"], details=empirical["rights"]))
    claims.append(_claim("france_purchase_entity_service", {"status": "unsupported"},
                         ["sha256:" + content_hash(empirical["regulatory_eligibility"])],
                         reasons=["Dated catalogue evidence does not verify the user's contracted entity and service"],
                         needed=["Verify contracted legal entity, France service and dated official evidence"],
                         details=empirical["regulatory_eligibility"]))
    if windows is None:
        claims.append(_claim("purchase_windows", {"status": "not_requested"}, [],
                             needed=["Request windows with explicit horizons and thresholds"]))
    else:
        for horizon, thresholds in windows["windows"].items():
            for threshold, result in thresholds.items():
                claims.append(_claim(f"purchase_windows.{horizon}d.{threshold}", result, refs,
                                     reasons=[result["insufficient_reason"]] if result["insufficient_reason"] else
                                     (["hourly_anchor_stale"] if result["status"] == "stale" else []),
                                     needed=[] if result["status"] == "experimental" else
                                     ["Contiguous feature history, disjoint outcomes and a fresh aligned anchor as required by analyze_windows"],
                                     demo=demo))
    for section_name in ("venue_disagreement", "venue_costs"):
        section = market[section_name]
        if section.get("status") == "not_requested":
            claims.append(_claim(section_name, section, [], needed=["Request explicit venue sources and analysis assumptions"]))
            continue
        claims.append(_claim(section_name, section, _refs(section), demo=demo,
                             needed=[] if _status(section["status"]) == "available" else
                             ["Fresh clock-aligned native book evidence and explicit analysis assumptions"],
                             details={key: value for key, value in section.items() if key != "stored_inputs"}))
        for row in section["rows"]:
            claims.append(_claim(section_name + "." + row["source"], row, _refs(section), demo=demo,
                                 needed=[] if _status(row["status"]) == "available" else
                                 ["Actual fresh ask/bid levels, full-book quality, declared fee schedule and spot product"]))
    derivatives = market["derivatives"]
    if derivatives.get("status") == "not_requested" or not derivatives.get("series"):
        claims.append(_claim("derivatives", derivatives if "status" in derivatives else {"status": "missing"},
                             _refs(derivatives), needed=["Explicit native actual/projected funding or OI identity and known endpoint observations"]))
    for index, series in enumerate(derivatives.get("series", [])):
        claims.append(_claim(f"derivatives.{index}.current", series, _refs(derivatives), demo=demo,
                             needed=[] if series["status"] == "ok" else ["Fresh native observation with known event and availability clocks"]))
        for horizon, comparison in series["comparisons"].items():
            claims.append(_claim(f"derivatives.{index}.{horizon}", comparison, _refs(derivatives), demo=demo,
                                 details={"identity": series["identity"], **comparison},
                                 needed=[] if comparison["status"] == "ok" else ["Both native endpoints within declared tolerance, known at each cutoff"]))
    quotes = market["quotes"]
    if quotes.get("status") == "not_requested":
        claims.append(_claim("quotes", quotes, [], needed=["Request explicit FX/stablecoin sources"]))
    else:
        claims.append(_claim("quotes.fx_reference", quotes["fx_reference"], _refs(quotes), demo=demo,
                             needed=[] if quotes["fx_reference"]["status"] == "available" else
                             ["Fresh indicative FX reference with a known source clock; date-only releases cannot establish an intraday endpoint"]))
        for index, row in enumerate(quotes["stablecoin_basis"]):
            claims.append(_claim(f"quotes.stablecoin_basis.{index}", row, _refs(quotes), demo=demo,
                                 needed=[] if row["status"] == "available" else
                                 ["Fresh directly observed native stablecoin price and, for EUR, clock-aligned valid FX reference"]))
        if not quotes["stablecoin_basis"]:
            claims.append(_claim("quotes.stablecoin_basis", {"status": "missing"}, _refs(quotes),
                                 needed=["Direct USDT or USDC native spot observations from selected sources"]))
    return claims


def _actions(markets, mode):
    actions, seen = [], set()

    def add(kind, selection, reasons, command, arguments=None, required=None):
        key = (kind, selection["asset"], selection["quote"], selection["source"])
        if key in seen:
            return
        seen.add(key)
        actions.append({"kind": kind, "selection": selection, "reasons": reasons,
                        "command": command, "arguments": arguments or {}, "required_user_inputs": required or [],
                        "execute": False})

    for market in markets:
        selection, claims = market["selection"], market["claims"]
        history = next((c for c in claims if c["claim"] == "empirical_hourly_history"), None)
        anchor = next((c for c in claims if c["claim"] == "hourly_anchor"), None)
        if anchor and anchor["status"] in ("missing", "stale"):
            command = "crypto-market-analysis collect" if mode == "live" else "crypto-market-analysis sources"
            arguments = {"--venues": selection["source"], "--quotes": selection["quote"],
                         "--tasks": "market", "--days": 1, "--max-pages": 1, "--mode": "live"} if mode == "live" else {}
            add("data_refresh", selection, anchor["reasons"], command, arguments,
                required=["Verify source adapter capability and runtime health before explicit collection",
                          "Import mode requires the configured dataset refresh; demo history cannot qualify real data"])
        if history and history["status"] not in ("available", "demo_only"):
            coverage = history["details"]["coverage"]
            if coverage.get("missing_hours"):
                arguments = {"--asset": selection["asset"], "--quote": selection["quote"],
                             "--start": coverage["required_start"], "--end": coverage["required_end"]}
                command = "crypto-market-analysis sources"
                if selection["source"] == "kraken" and mode == "import":
                    command = "crypto-market-analysis history-dataset plan-repair"
                else:
                    arguments = {}
                add("recover_recent_gap", selection, ["required_hourly_history_missing"], command, arguments,
                    required=["Supply --dataset-config for the selected Kraken import dataset when using its repair planner",
                              "Inspect source catalogue retention and the bounded repair planner; recoverability is not established here",
                              "Apply only ranges the source-specific workflow declares recoverable"])
                add("import_archives", selection, ["history_outside_verified_API_recovery_may_require_archives"],
                    "crypto-market-analysis import-history", {"--source": selection["source"], "--asset": selection["asset"], "--quote": selection["quote"]},
                    ["Supply --file from a sourced archive and verify its checksum, native identity and intended-use rights",
                     "Archive requirement is conditional on the source catalogue/repair contract, not inferred from candle count"])
        for claim in claims:
            if claim["claim"].startswith("venue_costs.") and claim["status"] != "available":
                source = claim["details"].get("source", selection["source"])
                add("declare_fees/product", {**selection, "source": source}, claim["reasons"],
                    "crypto-market-analysis purchase-costs", required=["Supply explicit request with native budget, actual book, fee assumptions and spot product"])
            if claim["claim"] in ("data_reuse_permission", "france_purchase_entity_service"):
                add("review_rights/entity", selection, ["Rights and contracted entity/service acceptance remain independent"],
                    "crypto-market-analysis sources", required=["Review intended reuse and contracted legal entity/service for France"])
        if any(c["status"] == "not_requested" for c in claims):
            add("request_specific_analysis", selection, ["Optional analytical sections were not requested"],
                "crypto-market-analysis analysis-readiness", required=["Supply --request selecting desired native analyses and their explicit assumptions"])
        if mode == "demo":
            add("request_specific_analysis", selection, ["Synthetic evidence supports demonstrations only"],
                "crypto-market-analysis analysis-readiness", required=["Select a separate live or import native evidence request"])
    order = {name: index for index, name in enumerate(("data_refresh", "recover_recent_gap", "import_archives",
             "declare_fees/product", "review_rights/entity", "wait_for_outcome", "request_specific_analysis"))}
    return sorted(actions, key=lambda row: order[row["kind"]])


def _finish(result):
    result["input_hash"] = content_hash(result)
    result["snapshot_id"] = result["input_hash"][:24]
    encoded = canonical_json(result)
    if len(encoded.encode()) > MAX_RESULT_BYTES:
        raise ValueError("Analysis readiness exceeds its 32 MiB complete-result budget; narrow the request.")
    return json.loads(encoded)


def build_analysis_readiness(store: Store, request: dict, as_of: int | None = None) -> dict:
    """Diagnose 1–4 native selections using the existing calculators and one session.

    The dossier retains exact consumed candles; optional windows reuse them rather
    than querying a second history. Qualification selectors derive exclusively
    from the dossier. Missing databases produce a structured result without init.
    """
    cutoff = bounded_as_of(as_of)
    if cutoff > 253402300799:
        raise ValueError("Readiness as_of is outside the supported UTC range.")
    request, qualification_request = _request(request, cutoff)
    mode = request["dossier"]["data_mode"]
    base = {"schema_version": 1, "as_of": cutoff, "data_mode": mode, "request": request,
            "read_only": True, "requests_performed": 0,
            "interpretation": "Evidence-supported analytical scope only; no financial edge, confidence score or purchase ranking.",
            "omissions": ["Archive contents and live network availability are not inspected.",
                          "Future issued-window outcomes are not requested or inferred from retrospective frequencies.",
                          "Data permission acceptance and contracted France entity/service verification remain external decisions."],
            "resource_limits": {"request_bytes": 65536, "result_bytes": MAX_RESULT_BYTES, "next_actions": 128}}
    if not store.path.is_file():
        markets = [{"selection": {**{k: row[k] for k in ("asset", "source", "quote")}, "data_mode": mode},
                    "claims": [_claim("stored_evidence", {"status": "missing"}, [], reasons=["configured_database_missing"],
                                      needed=["Initialize an explicit local store and import or collect native evidence"])]}
                   for row in request["dossier"]["markets"]]
        return _finish({**base, "status": "missing", "watermark": None, "dossier": None, "qualification": None,
                        "markets": markets, "calendar": {"status": "missing", "reason": "configured_database_missing"},
                        "next_actions": [{"kind": "request_specific_analysis", "command": "crypto-market-analysis sources",
                                          "arguments": {}, "required_user_inputs": ["Select and initialize the intended evidence store explicitly"],
                                          "reasons": ["configured_database_missing"], "execute": False}]})
    with store.read_session() as session:
        dossier = build_analysis_dossier(session, request["dossier"], cutoff)
        qualification = qualify_sources(session, qualification_request, cutoff)
        markets = []
        for market in dossier["markets"]:
            selection = market["selection"]
            empirical = next(row for row in qualification["markets"] if all(
                row[key] == selection[key] for key in ("asset", "quote", "source")))
            windows = None
            if "windows" in request:
                captured = market["market_context"]["stored_inputs"]["consumed_records"]
                windows = analyze_windows([Candle(**row) for row in captured], cutoff, **request["windows"])
            claims = _market_claims(market, empirical, windows, mode == "demo", cutoff, request["freshness_seconds"])
            markets.append({"selection": selection, "claims": claims, "windows": windows})
        calendar = dossier["calendar"]
        calendar_claim = _claim("upcoming_calendar", calendar, _refs(calendar), demo=mode == "demo",
                                reasons=["Rows describe known schedules; empty rows do not establish that no events exist"],
                                needed=["Explicit source/date-range coverage and known release clocks"],
                                details={"selection": calendar["selection"], "rows": calendar["rows"],
                                         "coverage_status": "not_established_by_event_rows"})
        # A finite event list is useful but does not certify complete calendar coverage.
        if calendar_claim["status"] == "available":
            calendar_claim["status"] = "incomplete"
        data_statuses = [claim["status"] for market in markets for claim in market["claims"]
                         if claim["claim"] not in ("data_reuse_permission", "france_purchase_entity_service", "source_runtime_access")
                         and claim["status"] != "not_requested"]
        status = "demo_only" if mode == "demo" else "available" if all(s == "available" for s in data_statuses) else "incomplete"
        result = {**base, "status": status, "watermark": deepcopy(session.watermark_at_start),
                  "snapshot_basis": dossier["snapshot_basis"], "dossier": dossier, "qualification": qualification,
                  "markets": markets, "calendar": calendar_claim, "next_actions": _actions(markets, mode)}
        return _finish(result)


def render_analysis_readiness_markdown(result: dict) -> str:
    """Render frozen claims without reading storage or recalculating support."""
    lines = ["# Analysis readiness", "", f"Status: **{result['status']}**. Mode: {result['data_mode']}. As of: {result['as_of']}.",
             "", result["interpretation"], ""]
    for market in result["markets"]:
        identity = market["selection"]
        lines.extend([f"## {identity['asset']}/{identity['quote']} — {identity['source']}", ""])
        for claim in market["claims"]:
            reason = "; ".join(claim["reasons"] or claim["needed_inputs"])
            lines.append(f"- {claim['claim']}: **{claim['status']}**" + (f" — {reason}" if reason else ""))
        lines.append("")
    calendar = result["calendar"]
    lines.extend(["## Calendar", "", "Status: **" + calendar["status"] + "**.", "",
                  "## Next actions", ""])
    for row in result["next_actions"]:
        arguments = shlex.join([str(value) for item in row["arguments"].items() for value in item])
        command = row["command"] + (" " + arguments if arguments else "")
        lines.append(f"- {row['kind']}: `{command}` — {'; '.join(row['required_user_inputs'])}")
    lines.extend(["", "## Omissions", ""])
    lines.extend("- " + omission for omission in result["omissions"])
    lines.extend(["", "Snapshot: " + result["snapshot_id"],
                  "", "Physical watermark: `" + canonical_json(result["watermark"]) + "`", ""])
    return "\n".join(lines)
