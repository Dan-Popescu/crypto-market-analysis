"""Bounded changes between two known-at cutoffs in one physical read snapshot."""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, InvalidOperation

from .analysis_dossier import MAX_CANDLES, _validate, build_analysis_dossier
from .derivatives_context import _dimension
from .evidence import canonical_json, content_hash
from .freshness import assess_freshness
from .models import Observation
from .reporting import bounded_as_of, bounded_object
from .storage import _LOCAL_RECORD_VERSION_ORDER, Store

MAX_CHANGES = 500
MAX_TOTAL_CANDLES = 2 * MAX_CANDLES
MAX_RAW_RECORDS = 250_000
MAX_RESULT_BYTES = 4 * 1024 * 1024


def _semantic(value):
    """Compare numeric values, keeping the original representation in evidence."""
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
            if number.is_finite():
                return number
        except InvalidOperation:
            pass
    return value


def _fact(value, *, identity, event=None, known=None, refs=(), unit=None, status=None):
    return {"value": value, "identity": identity, "unit": unit, "status": status,
            "event_time": event, "known_at": known, "input_references": list(refs)}


def _facts(snapshot):
    facts = {}
    for index, market in enumerate(snapshot["markets"]):
        native = market["selection"]
        context = market["market_context"]
        records = context["stored_inputs"]["consumed_records"]
        last = max(records, key=lambda row: row["open_time"], default=None)
        reference = ["sha256:" + content_hash(last)] if last else []
        event = last["close_time"] if last else None
        known = last["observed_at"] if last else None
        prefix = f"markets/{index}"
        facts[prefix + "/price_anchor"] = _fact(
            context["anchor"]["price"] if context["anchor"] else None, identity=native,
            event=event, known=known, refs=reference, unit=native["quote"] + " per " + native["asset"],
            status="available" if last else "missing")
        coverage = {key: context["coverage"][key] for key in
                    ("complete_days", "eligible_hours", "start", "end")}
        facts[prefix + "/coverage"] = _fact(coverage, identity=native, event=event, known=known,
                                                   refs=reference, status=context["status"])
        fresh = assess_freshness(event, known, snapshot["as_of"], 3600)
        facts[prefix + "/freshness"] = _fact(
            {"status": fresh["status"], "expires_at": fresh["expires_at"], "max_age_seconds": 7200},
            identity=native, event=event, known=known, refs=reference, status=fresh["status"])
        for name in ("venue_disagreement", "venue_costs"):
            panel = market[name]
            if panel.get("status") == "not_requested":
                continue
            for row in panel["rows"]:
                timing = row.get("timing") or {}
                identity = {**native, "source": row["source"], "section": name}
                value = {key: row[key] for key in
                         ("price", "best_bid", "best_ask", "spread_quote", "spread_bps", "captured_depth",
                          "hypothetical_fill", "product", "assumptions", "purchase_eligibility", "reasons") if key in row}
                # Fills are already summarized by quantities, costs and captured depth.
                if isinstance(value.get("hypothetical_fill"), dict):
                    value["hypothetical_fill"] = {key: item for key, item in value["hypothetical_fill"].items()
                                                    if key != "fills"}
                facts[prefix + "/" + name + "/" + row["source"]] = _fact(
                    value, identity=identity, event=timing.get("event_time"), known=timing.get("captured_at"),
                    refs=[row["input_reference"]] if row.get("input_reference") else [],
                    unit={"price": native["quote"] + " per " + native["asset"], "quantity": native["asset"],
                          "cost": native["quote"], "spread_bps": "basis_points"}, status=row["status"])
        panel = market["derivatives"]
        if panel.get("status") != "not_requested":
            for series in panel["series"]:
                identity = series["identity"]
                point = series["current"] or {}
                refs = [record["input_reference"] for record in panel["stored_inputs"]["consumed_records"]
                        if point and all(record["payload"].get(key) == point.get(key)
                                         for key in ("source", "asset", "metric", "quote", "unit", "event_time", "observed_at", "metadata"))]
                key = prefix + "/derivatives/" + canonical_json(identity)
                facts[key] = _fact(point.get("value"), identity=identity, event=point.get("event_time"),
                                   known=point.get("observed_at"), refs=refs, unit=point.get("unit"),
                                   status=series["status"])
        panel = market["quotes"]
        if panel.get("status") != "not_requested":
            invalid = {row["input_reference"]: row["status"] for row in panel["stored_inputs"]["unknown_inputs"]}
            for record in panel["stored_inputs"]["consumed_records"]:
                row = record["payload"]
                identity = {key: row.get(key) for key in ("source", "base", "quote", "data_mode", "price_type")}
                identity["kind"] = record["kind"]
                event, known = row.get("event_time"), row.get("observed_at")
                status = invalid.get(record["input_reference"])
                if status is None:
                    status = ("unknown_timestamp" if event is None or known is None else
                              "stale" if snapshot["as_of"] - event > panel["limits"]["max_age_seconds"] else "available")
                facts[prefix + "/quotes/" + canonical_json(identity)] = _fact(
                    {"native_value": row.get("value"), "data_version": row.get("data_version"),
                     "source_url": row.get("source_url")}, identity=identity, event=event, known=known,
                    refs=[record["input_reference"]], unit=str(row.get("quote")) + " per " + str(row.get("base")),
                    status=status)
    return facts


def _candle_versions(snapshot):
    rows = {}
    for index, market in enumerate(snapshot["markets"]):
        for row in market["market_context"]["stored_inputs"]["consumed_records"]:
            rows[index, row["open_time"]] = row
    return rows


def _candle_fact(row):
    if row is None:
        return None
    return _fact({key: row[key] for key in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote_volume")},
                 identity={key: row[key] for key in ("source", "asset", "quote", "data_mode", "timeframe")},
                 event=row["close_time"], known=row["observed_at"], refs=["sha256:" + content_hash(row)],
                 unit={"prices": row["quote"] + " per " + row["asset"], "volume": row["asset"],
                       "quote_volume": row["quote"], "taker_buy_quote_volume": row["quote"]}, status="available")


def _category(before, after, previous):
    if after is None or (after["value"] is None and after["status"] in ("missing", "unavailable", "missing_endpoint")):
        return "missing_or_retained_history_unavailable"
    if before is None or (before["value"] is None and before["status"] in ("missing", "unavailable", "missing_endpoint")):
        return "late_arrival" if after["event_time"] is not None and after["event_time"] <= previous else "newly_available"
    values_equal = _semantic(before["value"]) == _semantic(after["value"])
    same_event = before["event_time"] == after["event_time"]
    if same_event and values_equal and before["status"] == after["status"]:
        return "receipt_only_update"
    if same_event and before["known_at"] == after["known_at"] and before["input_references"] == after["input_references"]:
        return "clock_or_expiry_change"
    if same_event:
        return "same_event_correction"
    if after["event_time"] is not None and after["event_time"] <= previous:
        return "late_arrival"
    return "new_event_observation"


def _change(path, before, after, previous):
    category = _category(before, after, previous)
    # A numerical price change requires comparable, fresh, newly evented points.
    movement = (path.endswith("/price_anchor") and category == "new_event_observation"
                and before is not None and after is not None
                and before["value"] is not None and after["value"] is not None
                and _semantic(before["value"]) != _semantic(after["value"]))
    return {"path": path, "category": category, "market_movement": movement,
            "before": before, "after": after}


def _preflight(session, request, cutoffs):
    count = 0
    with session.connection() as con:
        for cutoff in cutoffs:
            for market in request["markets"]:
                count += con.execute("""SELECT COUNT(*) FROM (SELECT timeframe,open_time FROM candles
                    WHERE asset=? AND source=? AND quote=? AND data_mode=? AND close_time<=? AND observed_at<=?
                    GROUP BY timeframe,open_time)""", (market["asset"], market["source"], market["quote"],
                    request["data_mode"], cutoff, cutoff)).fetchone()[0]
                if count > MAX_TOTAL_CANDLES:
                    raise ValueError("Analysis changes exceeds its shared 200000-candle budget across both cutoffs.")
    return count


def _health(session, request, cutoff):
    sources = {market["source"] for market in request["markets"]}
    for market in request["markets"]:
        for section in ("venues", "quotes"):
            sources.update(market.get(section, {}).get("sources", []))
        if "derivatives" in market:
            sources.add(market["derivatives"]["source"])
    if request.get("calendar", {}).get("source"):
        sources.add(request["calendar"]["source"])
    placeholders = ",".join("?" for _ in sources)
    with session.connection() as con:
        rows = con.execute(f"""WITH ranked AS (SELECT id,source,task,observed_at,payload,
            ROW_NUMBER() OVER(PARTITION BY source,task ORDER BY observed_at DESC,id DESC) rn FROM runs
            WHERE observed_at<=? AND json_extract(payload,'$.data_mode')=? AND source IN ({placeholders}))
            SELECT id,source,task,observed_at,payload FROM ranked WHERE rn=1 ORDER BY source,task LIMIT 257""",
            (cutoff, request["data_mode"], *sorted(sources))).fetchall()
        if len(rows) > 256:
            raise ValueError("Analysis changes collection health exceeds 256 source/task pairs.")
        unknown = con.execute(f"""SELECT COUNT(*) FROM runs WHERE observed_at<=?
            AND source IN ({placeholders}) AND json_extract(payload,'$.data_mode') IS NULL""",
            (cutoff, *sorted(sources))).fetchone()[0]
    return {"as_of": cutoff, "rows": [{"source": row["source"], "task": row["task"],
            "known_at": row["observed_at"], "input_reference": f"sqlite:runs:{row['id']}",
            "errors": json.loads(row["payload"]).get("errors", []),
            "status": "error" if json.loads(row["payload"]).get("errors") else "no_recorded_error"}
            for row in rows], "excluded_mode_unknown_runs": unknown,
            "basis": "latest explicitly mode-labelled persisted run per selected source/task; success does not establish freshness"}


def _calendar(session, request, snapshot, previous_identities=()):
    """Retain cancellations and revisions inside the requested calendar range."""
    if "calendar" not in request:
        return {}
    bounds = snapshot["calendar"]["range"]
    source = request["calendar"].get("source")
    with session.connection() as con:
        placeholders = ",".join("?" for _ in previous_identities) or "NULL"
        rows = con.execute(f"""WITH ranked AS (SELECT identity,payload,ROW_NUMBER() OVER(PARTITION BY identity
            ORDER BY {_LOCAL_RECORD_VERSION_ORDER}) rn FROM local_records WHERE kind='calendar' AND observed_at<=?)
            SELECT identity,payload FROM ranked WHERE rn=1 AND (? IS NULL OR json_extract(payload,'$.source')=?)
            AND (json_extract(payload,'$.data_mode')=? OR json_extract(payload,'$.data_mode') IS NULL)
            AND (identity IN ({placeholders}) OR json_extract(payload,'$.event_time') BETWEEN ? AND ? OR (json_extract(payload,'$.event_time') IS NULL
            AND COALESCE(json_extract(payload,'$.local_date'),json_extract(payload,'$.date'))
            BETWEEN date(?,'unixepoch') AND date(?,'unixepoch'))) ORDER BY identity LIMIT 201""",
            (snapshot["as_of"], source, source, request["data_mode"], *previous_identities, bounds["start"], bounds["end"],
             bounds["start"], bounds["end"])).fetchall()
    if len(rows) > 200:
        raise ValueError("Analysis changes calendar exceeds 200 retained revisions; narrow its range.")
    return {row["identity"]: json.loads(row["payload"]) for row in rows}


def _derivative_versions(snapshot):
    rows = {}
    for index, market in enumerate(snapshot["markets"]):
        for record in market["derivatives"].get("stored_inputs", {}).get("consumed_records", []):
            row = record["payload"]
            identity = {key: row.get(key) for key in ("source", "asset", "metric", "quote", "unit", "data_mode")}
            identity["dimension"] = _dimension(Observation(**row))
            key = index, canonical_json(identity), row["event_time"]
            if key not in rows or (row["observed_at"], record["input_reference"]) > (
                    rows[key]["payload"]["observed_at"], rows[key]["input_reference"]):
                rows[key] = record
    return rows


def _derivative_fact(record):
    if record is None:
        return None
    row = record["payload"]
    identity = {key: row.get(key) for key in ("source", "asset", "metric", "quote", "unit", "data_mode")}
    identity["dimension"] = _dimension(Observation(**row))
    return _fact(row["value"], identity=identity, event=row["event_time"], known=row["observed_at"],
                 refs=[record["input_reference"]], unit=row.get("unit"), status=row.get("metadata", {}).get("status", "observed"))


def _calendar_fact(row):
    if row is None:
        return None
    return _fact({key: value for key, value in row.items() if key != "observed_at"},
                 identity={key: row.get(key) for key in ("event_id", "source", "data_mode")},
                 event=row.get("event_time"), known=row.get("observed_at"),
                 refs=["sha256:" + content_hash(row)], status=row.get("status", "planned"))


def build_analysis_changes(store: Store, request: dict, as_of: int | None = None) -> dict:
    """Compare requested native sections at two cutoffs without collection or writes.

    Both known-at reconstructions use one bounded physical SQLite snapshot. This
    cannot reconstruct history already deleted before this operation began.
    """
    bounded_object(request, "analysis changes request")
    if set(request) - {"dossier", "previous_as_of", "max_changes"} or "previous_as_of" not in request:
        raise ValueError("Analysis changes requires dossier and previous_as_of with optional max_changes.")
    dossier = _validate(request.get("dossier"))
    previous, current = bounded_as_of(request["previous_as_of"]), bounded_as_of(as_of)
    if previous >= current or current > 253402300799:
        raise ValueError("Analysis changes requires previous_as_of strictly before the supported as_of cutoff.")
    maximum = request.get("max_changes", 100)
    if type(maximum) is not int or not 1 <= maximum <= MAX_CHANGES:
        raise ValueError("max_changes must be an integer from 1 to 500.")
    with store.read_session() as session:
        candles = _preflight(session, dossier, (previous, current))
        before = build_analysis_dossier(session, dossier, previous)
        after = build_analysis_dossier(session, dossier, current)
        raw_count = sum(len(section.get("stored_inputs", {}).get("consumed_records", []))
                        for snapshot in (before, after) for market in snapshot["markets"]
                        for section in market.values() if isinstance(section, dict))
        if raw_count > MAX_RAW_RECORDS:
            raise ValueError("Analysis changes exceeds its shared 250000 raw-record budget.")
        health = {"before": _health(session, dossier, previous), "after": _health(session, dossier, current)}
        previous_calendar = _calendar(session, dossier, before)
        calendars = (previous_calendar, _calendar(session, dossier, after, tuple(previous_calendar)))
        old_facts, new_facts = _facts(before), _facts(after)
        changes = []
        for key in sorted(old_facts.keys() | new_facts.keys()):
            old, new = old_facts.get(key), new_facts.get(key)
            if _semantic(old) != _semantic(new):
                changes.append(_change(key, old, new, previous))
        old_rows, new_rows = _candle_versions(before), _candle_versions(after)
        new_event_count = 0
        for key in sorted(old_rows.keys() | new_rows.keys()):
            old, new = old_rows.get(key), new_rows.get(key)
            if old is None and new is not None and new["close_time"] > previous:
                new_event_count += 1
                continue  # The newest price anchor summarizes new hourly observations.
            if _semantic(old) != _semantic(new):
                changes.append(_change(f"markets/{key[0]}/candle/{key[1]}", _candle_fact(old),
                                       _candle_fact(new), previous))
        old_derivatives, new_derivatives = _derivative_versions(before), _derivative_versions(after)
        for key in sorted(old_derivatives.keys() | new_derivatives.keys(), key=str):
            old, new = old_derivatives.get(key), new_derivatives.get(key)
            if old is None and new is not None and key[2] > previous:
                continue
            if _semantic(old) != _semantic(new):
                change = _change(f"markets/{key[0]}/derivative_vintage/{key[1]}/{key[2]}",
                                 _derivative_fact(old), _derivative_fact(new), previous)
                if new is None and key[2] < max(0, current - 172800):
                    change["category"] = "clock_or_range_exit"
                changes.append(change)
        for key in sorted(calendars[0].keys() | calendars[1].keys()):
            old, new = calendars[0].get(key), calendars[1].get(key)
            if _semantic(old) == _semantic(new):
                continue
            change = _change("calendar/" + key, _calendar_fact(old), _calendar_fact(new), previous)
            change["category"] = ("receipt_only_update" if old and new and _semantic(change["before"]["value"]) == _semantic(change["after"]["value"])
                                  else "calendar_revision" if old and new else "calendar_newly_available" if new else "clock_or_range_exit")
            changes.append(change)
        # Keep real observations/corrections first, followed by operational/time changes.
        changes.sort(key=lambda row: (row["category"] in ("receipt_only_update", "clock_or_expiry_change"), row["path"]))
        counts = Counter(row["category"] for row in changes)
        retained = changes[:maximum]
        markets = []
        for index, selection in enumerate(dossier["markets"]):
            prefix = f"markets/{index}/"
            selected_changes = [row for row in changes if row["path"].startswith(prefix)]
            panels = {}
            for label, facts in (("before", old_facts), ("after", new_facts)):
                panels[label] = {name: facts[prefix + name] for name in ("price_anchor", "coverage", "freshness")}
            markets.append({"selection": before["markets"][index]["selection"],
                            "requested_sections": ["market_context", *[name for name in ("venues", "derivatives", "quotes") if name in selection]],
                            "total_changes": len(selected_changes),
                            "by_category": dict(sorted(Counter(row["category"] for row in selected_changes).items())),
                            "market_movements": sum(row["market_movement"] for row in selected_changes), **panels})
        result = {"schema_version": 1, "kind": "native_analysis_changes", "previous_as_of": previous,
                  "as_of": current, "data_mode": dossier["data_mode"], "request": request,
                  "watermark": session.watermark_at_start, "read_only": True,
                  "summary": {"total_changes": len(changes), "by_category": dict(sorted(counts.items())),
                              "market_movements": sum(row["market_movement"] for row in changes),
                              "new_hourly_observations": new_event_count,
                              "collection_errors_before": sum(row["status"] == "error" for row in health["before"]["rows"]),
                              "collection_errors_after": sum(row["status"] == "error" for row in health["after"]["rows"])},
                  "markets": markets, "changes": retained, "collection_health": health,
                  "snapshots": {"before": {"as_of": previous, "input_hash": before["input_hash"], "snapshot_id": before["snapshot_id"]},
                                "after": {"as_of": current, "input_hash": after["input_hash"], "snapshot_id": after["snapshot_id"]}},
                  "omissions": {"changes": len(changes) - len(retained), "truncated": len(changes) > len(retained)},
                  "resource_limits": {"total_candles_both_cutoffs": MAX_TOTAL_CANDLES,
                                      "candles_per_snapshot": MAX_CANDLES, "consumed_candles": candles,
                                      "total_raw_records": MAX_RAW_RECORDS, "consumed_raw_records": raw_count,
                                      "max_changes": maximum, "result_bytes": MAX_RESULT_BYTES,
                                      "sqlite_returned_bytes": 64 * 1024 * 1024, "sqlite_vm_steps": 50_000_000,
                                      "read_session_seconds": 15},
                  "limitations": ["Two known-at reconstructions share one present physical snapshot; deleted or archived evidence is unknown.",
                                  "Native currencies, sources, units, contracts, actual/projected funding and modes remain separate.",
                                  "Clock, coverage, receipt and collection health changes do not establish a market movement.",
                                  "Hourly freshness uses a stated two-hour lifetime; no conversion, assumed fees or trading recommendation.",
                                  "Snapshot hashes include cutoffs and are references, never change predicates."]}
        # Omit a deterministic suffix until the compact result fits, preserving exact counts.
        while len(canonical_json(result).encode()) > MAX_RESULT_BYTES - 100 and result["changes"]:
            result["changes"].pop()
            result["omissions"] = {"changes": len(changes) - len(result["changes"]), "truncated": True}
        if len(canonical_json(result).encode()) > MAX_RESULT_BYTES - 100:
            raise ValueError("Analysis changes fixed metadata exceeds its 4 MiB result budget.")
        result["input_hash"] = content_hash(result)
        return json.loads(canonical_json(result))


def render_analysis_changes_markdown(result: dict) -> str:
    """Render compact evidence with explicit unknowns and no trading advice."""
    summary = result["summary"]
    lines = ["# Native analysis changes", "", f"UTC cutoffs: {result['previous_as_of']} → {result['as_of']}.", "",
             f"{summary['total_changes']} changes; {summary['market_movements']} newly evented native price movements; "
             f"{result['omissions']['changes']} omitted changes.", "",
             f"Collection errors: {summary['collection_errors_before']} → {summary['collection_errors_after']} source/task pairs.", ""]
    for row in result["changes"]:
        old = canonical_json(row["before"]["value"]) if row["before"] else "unknown"
        new = canonical_json(row["after"]["value"]) if row["after"] else "unknown"
        point = row["after"] or row["before"]
        lines.append(f"- {row['path']} ({row['category']}): {old} → {new}; "
                     f"identity={canonical_json(point['identity'])}; unit={canonical_json(point['unit'])}; "
                     f"known_at={point['known_at']}; references={canonical_json(point['input_references'])}")
    lines.extend(["", "Native quotes and units remain separate. Missing evidence stays unknown. No trading recommendation."])
    return "\n".join(lines) + "\n"
