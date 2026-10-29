"""Portable, finite public capture and offline native-market qualification.

Inspection never initializes a database. Refresh is an explicit current-clock,
live-only one-shot operation; older history belongs to the archive/dataset route.
This composes the existing local capture and stored qualification contracts and
does not issue assessments or start a worker.
"""

from __future__ import annotations

import hashlib
import platform
import sqlite3
import subprocess
import time
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import __version__
from .collectors import ExchangeClients
from .config import Settings
from .engine import Engine
from .evidence import content_hash
from .http_accounting import merge_measurements
from .local_workflow import capture_local_inputs, normalize_local_workflow_request
from .qualification import qualify_sources
from .storage_policy import physical_storage

_FIELDS = {
    "refresh", "data_mode", "as_of", "sources", "markets", "market_sources",
    "required_history_days", "freshness_seconds", "max_gap_hours", "max_gap_details",
    "max_evidence_rows", "max_query_rows", "max_storage_bytes", "max_pages", "history_days",
    "max_logical_operations",
}
_QUALITY_FIELDS = {
    "sources", "markets", "market_sources", "data_mode", "required_history_days",
    "freshness_seconds", "max_gap_hours", "max_gap_details", "max_evidence_rows",
    "max_query_rows", "max_storage_bytes",
}


def _request(settings, request):
    if not isinstance(request, dict) or set(request) - _FIELDS:
        raise ValueError("Unknown installation qualification request fields.")
    defaults = {
        "sources": ["kraken"], "history_days": 7, "max_pages": 2,
        "max_logical_operations": 64, "max_query_rows": 250_000,
        "max_storage_bytes": min(settings.storage_hot_max_bytes, 512 * 1024**2),
    }
    normalized = normalize_local_workflow_request(settings, {**defaults, **request})
    if len(normalized["sources"]) > 4:
        raise ValueError("Installation qualification supports at most four explicit sources.")
    if normalized["history_days"] > 30 or normalized["max_pages"] > 4:
        raise ValueError("Capture is limited to 30 recent days and four pages per native market.")
    if normalized["max_logical_operations"] > 256:
        raise ValueError("Installation qualification supports at most 256 logical operations.")
    return normalized


def _software():
    try:
        ccxt_version = version("ccxt")
    except PackageNotFoundError:
        ccxt_version = "unavailable"
    directory = Path(__file__).parent
    files = ("installation_qualification.py", "local_workflow.py", "qualification.py",
             "collectors.py", "pipeline.py", "storage.py", "source_catalog.py", "http_accounting.py")
    revision = None
    dirty = None
    try:
        if directory.parent.name == "src" and (directory.parent.parent / ".git").exists():
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=directory, capture_output=True,
                                    text=True, timeout=2, check=False)
            if result.returncode == 0:
                revision = result.stdout.strip()
                state = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=directory,
                                       capture_output=True, timeout=2, check=False)
                dirty = state.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "application_version": __version__, "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version, "ccxt_version": ccxt_version,
        "source_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                          for name in files},
        "checkout_revision": revision, "checkout_tracked_changes": dirty,
        "revision_basis": "SHA256 of the executed workflow, collector, storage and qualification modules.",
    }


def _capture_access(capture, market, source, injected):
    if not capture.get("network_requested"):
        return {"status": "not_requested", "last_success": None, "last_failure": None}
    captured = next((row for row in (capture.get("market") or {}).get("markets", [])
                     if row["market"] == market), {})
    run = next((row for row in captured.get("reports", []) if row["source"] == source), None)
    if run is None:
        return {"status": "unknown", "last_success": None, "last_failure": None,
                "errors": capture.get("errors", [])}
    success = bool(run.get("candles"))
    failure = bool(run.get("errors"))
    return {
        "status": "partial" if success and failure else "success" if success else "failed" if failure else "unknown",
        "last_success": run["observed_at"] if success else None,
        "last_failure": run["observed_at"] if failure else None,
        "errors": run.get("errors", []), "warnings": run.get("warnings", []),
        "evidence_basis": "injected_client_unqualified" if injected else "public_adapter_capture",
        "provider_acceptance": "unverified_injected_transport" if injected else "observed_for_this_capture_only",
        "scope": "This run, source and native market; no cross-market success inference.",
    }


def run_installation_qualification(settings: Settings, request: dict, *, clients=None,
                                   clock=time.time) -> dict:
    """Return a self-contained report; only explicit ``refresh=true`` captures.

    A missing or incompatible store yields unavailable inspection evidence without
    creating or migrating it. ``clock`` controls inspection cutoffs only. Actual
    public refresh uses the wall clock, never ``as_of`` or an injected clock.
    Named import datasets remain read-only and keep their existing identity.
    Resource limits are admission/query budgets, not a filesystem or wall-time
    quota. The caller owns injected clients and any report-file output.
    """
    started = int(time.time())
    timer = time.monotonic()
    request = _request(settings, request)
    as_of = int(time.time()) if request["refresh"] else request.get("as_of", int(clock()))
    if type(as_of) is not int or not 0 <= as_of <= int(time.time()):
        raise ValueError("The qualification clock must be a nonfuture UTC epoch second.")
    if as_of // 3600 * 3600 < request["required_history_days"] * 86400:
        raise ValueError("The requested history begins before UTC epoch zero.")
    marker = settings.data_dir / "dataset.json"
    if marker.exists() and (request["refresh"] or request["data_mode"] != "import"):
        raise ValueError("A named research dataset requires read-only import inspection; use its explicit repair workflow.")
    # Read the existing identity before considering initialization or public I/O.
    engine = Engine(settings, request["data_mode"], initialize=False)
    capture = {"status": "not_requested", "network_requested": False,
               "budget": {"logical_operations": 0, "catalogue_operations": 0,
                          "http_request_count": 0,
                          "max_logical_operations": request["max_logical_operations"]}}
    before = physical_storage(settings.database)
    if request["refresh"]:
        required_admission = before["hot_bytes"] + settings.storage_write_reserve_bytes + 64 * 1024
        if required_admission > min(settings.storage_hot_max_bytes, request["max_storage_bytes"]):
            capture.update(status="refused", errors=["Capture storage admission budget exceeded."],
                           refusal="storage_budget_exceeded")
        else:
            selected = [market.split("/") for market in request["markets"]]
            scoped = replace(
                settings, venues=request["sources"],
                assets=list(dict.fromkeys(asset for asset, _ in selected)),
                quote_currencies=list(dict.fromkeys(quote for _, quote in selected)),
                history_days=request["history_days"], max_pages=request["max_pages"],
                workers=1, options=False, context=False, liquidity_venues=None,
                storage_hot_max_bytes=min(settings.storage_hot_max_bytes, request["max_storage_bytes"]),
            )
            engine = Engine(scoped, "live")
            if clients is None:
                with ExchangeClients() as owned:
                    capture = capture_local_inputs(scoped, engine.store, request, owned)
            else:
                capture = capture_local_inputs(scoped, engine.store, request, clients)
            capture["network_requested"] = True
            as_of = int(time.time())
    # Always inspect through the read-only Store lifecycle, including after refresh.
    reader = Engine(settings, request["data_mode"], initialize=False)
    quality_request = {name: request[name] for name in _QUALITY_FIELDS if name in request}
    try:
        quality = qualify_sources(reader.store, quality_request, as_of)
    except ValueError as exc:
        quality = {"status": "unavailable", "markets": [], "as_of": as_of,
                   "reason": "store_unavailable", "error": str(exc), "watermark": None,
                   "resources": {"requests_performed": 0}}
    markets = []
    by_pair = {(row["market"], row["source"]): row for row in quality["markets"]}
    for market in request["markets"]:
        for source in request.get("market_sources", {}).get(market, request["sources"]):
            row = by_pair.get((market, source), {
                "market": market, "source": source, "data_mode": request["data_mode"],
                "status": "unavailable", "coverage": {"status": "unknown"},
                "freshness": {"status": "unknown"}, "reasons": ["store_unavailable"],
                "source_access": {"status": "untested"}, "rights": {"status": "unknown"},
                "regulatory_eligibility": {"jurisdiction": "France", "status": "unknown"},
            })
            markets.append({**row, "capture_access": _capture_access(capture, market, source, clients is not None)})
    status = quality["status"]
    if request["refresh"] and (capture.get("status") == "refused" or capture.get("errors")
                               or (capture.get("market") or {}).get("status") in ("error", "partial")
                               or any(row["capture_access"]["status"] != "success" for row in markets)):
        status = "partial" if status == "pass" else status
    normalized = {name: request[name] for name in sorted(_FIELDS) if name in request}
    capture_runs = [run for market in (capture.get("market") or {}).get("markets", [])
                    for run in market.get("reports", [])]
    http_accounting = merge_measurements([run.get("http_accounting") for run in capture_runs])
    http_accounting["network_requested"] = capture.get("network_requested", False)
    http_accounting["scope"] = "Observed transport work for this capture only; unknown totals do not mean zero requests."
    software = _software()
    report = {
        "schema_version": 1, "kind": "installation_qualification", "status": status,
        "as_of": as_of, "data_mode": request["data_mode"], "refresh": request["refresh"],
        "request": normalized, "dataset_lineage": reader.dataset_lineage,
        "collection": capture, "qualification": quality, "markets": markets,
        "run_manifest": {
            "started_at": started, "completed_at": int(time.time()),
            "elapsed_seconds": round(time.monotonic() - timer, 6),
            "cutoff_basis": "actual_wall_clock_after_capture" if capture.get("network_requested") else "inspection_cutoff",
            "knowledge_clock": "Stored observed_at is actual receipt availability, not historical publication time.",
            "request_sha256": content_hash(normalized), "software": software,
            "dataset_identity_sha256": hashlib.sha256(marker.read_bytes()).hexdigest() if marker.is_file() else None,
            "injected_clients": clients is not None,
        },
        "resources": {
            "before": before, "after": physical_storage(settings.database),
            "storage_admission_ceiling_bytes": min(settings.storage_hot_max_bytes, request["max_storage_bytes"]),
            "max_pages_per_source_native_market": request["max_pages"],
            "max_logical_operations": request["max_logical_operations"],
            "capture_history_days": request["history_days"], "workers": 1,
            "limits_basis": "Finite adapter/page/query and storage admission budgets; not wall-time or filesystem quotas.",
            "adapter_budget": capture.get("budget", {}),
            "http_accounting": http_accounting,
        },
        "older_history": {
            "kraken_recent_ohlc_limit_hours": 720,
            "pagination_extends_kraken_retention": False,
            "automatic_archive_download": False,
            "route": "Use history-dataset init/status/stage for locally supplied verified official archives; stage materializes the import history. Use plan-repair/repair for bounded recoverable gaps. Inspect that dataset using its data directory and import mode.",
            "identity_rule": "Never union live, import or synthetic histories, sources, or native quotes.",
        },
        "limitations": [
            "Coverage, runtime access, catalogue capability, reuse rights and France purchase eligibility are independent.",
            "A coverage pass establishes stored hourly history only; it does not approve redistribution or purchases.",
            "Injected transport acceptance does not establish real provider access.",
            "Recent API pagination cannot establish years of Kraken history; missing history remains unknown.",
            "Public live acceptance remains unverified until an explicit capture runs from an environment permitting the provider hosts.",
        ],
    }
    report["evidence_reference"] = "sha256:" + content_hash(report)
    return report


def render_installation_qualification(report: dict) -> str:
    """Render the bounded native-market summary; JSON retains the full evidence."""
    rows = ["# Installation qualification", "", f"Status: {report['status']}",
            f"UTC cutoff: {report['as_of']}; data mode: {report['data_mode']}; refresh: {report['refresh']}", "",
            "| Native market | Source | Coverage | Missing hours | Freshness | This capture |",
            "| --- | --- | --- | --- | --- | --- |"]
    for market in report["markets"]:
        rows.append(f"| {market['market']} | {market['source']} | {market['status']} | "
                    f"{market['coverage'].get('missing_hours', 'unknown')} | {market['freshness']['status']} | "
                    f"{market['capture_access']['status']} |")
    rows.extend(["", f"Evidence: {report['evidence_reference']}", "", *report["limitations"], ""])
    return "\n".join(rows)
