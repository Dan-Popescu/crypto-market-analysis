"""Read-only recovery verification of native originals and their query indices.

Hashes verify preservation, not an upstream publisher's truth. Import summaries
retain bundle digests, but do not contain original bundle bytes; those digests
are checked as lineage references rather than falsely claimed reconstructed.
"""

from __future__ import annotations

import json
import re
import sqlite3

from ..evidence import canonical_json, content_hash
from .identity import validate_registry
from .prices import number, validate_series
from .prospective import freeze_market_protocol
from .sessions import validate_calendar

_MAX_ROWS = 1_000_000
_MAX_BYTES = 512 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COLUMNS = {
    "imports": "hash received_at summary",
    "registries": "hash declared_at received_at known_at payload",
    "registry_components": "kind identity hash payload known_at received_at",
    "admissions": "kind identity known_at received_at import_hash",
    "calendars": "id version hash declared_at received_at known_at payload",
    "series": "id instrument_id listing_id provider native_currency unit price_basis data_mode calendar_id calendar_version interval known_at received_at registry_hash hash payload",
    "bars": "series_id start end observed_at known_at received_at hash payload import_hash",
    "actions": "series_id action_id effective_at observed_at known_at received_at hash payload import_hash",
    "series_imports": "hash series_id import_hash received_at bars actions known_at",
}
_PRIMARY = {
    "imports": ("hash",), "registries": ("hash",), "registry_components": ("kind", "identity"),
    "admissions": ("kind", "identity", "import_hash"), "calendars": ("id", "version"), "series": ("id",),
    "bars": ("series_id", "start", "end", "observed_at"), "actions": ("series_id", "action_id", "observed_at"),
    "series_imports": ("hash",), "assessments": ("id",),
}
_INDICES = {
    "registry_cutoff": ("registries", ("known_at",)),
    "component_cutoff": ("registry_components", ("known_at", "kind", "identity")),
    "admission_cutoff": ("admissions", ("kind", "identity", "known_at")),
    "series_cutoff": ("series", ("data_mode", "id", "known_at")),
    "bar_cutoff": ("bars", ("series_id", "start", "known_at", "observed_at")),
    "action_cutoff": ("actions", ("series_id", "known_at", "action_id")),
    "series_admission": ("series_imports", ("series_id", "known_at")),
}
_KEYS = {"instruments": ("id",), "listings": ("id", "known_at"),
         "aliases": ("provider", "symbol", "instrument_id", "listing_id", "effective_from", "known_at"),
         "memberships": ("universe", "instrument_id", "valid_from", "known_at")}


def _require(condition, message):
    if not condition:
        raise ValueError(f"Native market recovery verification failed: {message}")


def _clock(value):
    _require(type(value) is int and -62135596800 <= value <= 253402300799, "invalid indexed clock")


def _digest(value):
    _require(isinstance(value, str) and _DIGEST.fullmatch(value), "invalid content digest")


def _schema(con):
    schema = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    tables = {row[1] for row in schema if row[0] == "table"}
    names = {f"cross_asset_{name}" for name in _COLUMNS}
    _require(names <= tables, "incomplete native table contract")
    columns = dict(_COLUMNS)
    if "cross_asset_assessments" in tables:
        columns["assessments"] = "id idempotency_key payload"
    triggers = {row[1]: row[3] for row in schema if row[0] == "trigger"}
    for name, required in columns.items():
        table = f"cross_asset_{name}"
        info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        _require(set(required.split()) <= {row[1] for row in info}, f"incomplete columns for {table}")
        integers = {"start", "end", "observed_at", "known_at", "received_at", "declared_at", "effective_at"}
        if name == "series_imports":
            integers |= {"bars", "actions"}
        for field in info:
            if field[1] in required.split():
                _require(field[2].upper() == ("INTEGER" if field[1] in integers else "TEXT"),
                         f"invalid declared column type for {table}.{field[1]}")
        primary = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
        _require(primary == _PRIMARY[name], f"invalid primary key for {table}")
        if name == "assessments":
            unique = [row for row in con.execute(f'PRAGMA index_list("{table}")') if row[2]]
            _require(any(tuple(item[2] for item in con.execute(f'PRAGMA index_info("{row[1]}")')) == ("idempotency_key",)
                         for row in unique), "assessment idempotency uniqueness is missing")
        for operation in ("update", "delete"):
            guard = f"{table}_no_{operation}"
            sql = re.sub(r"\s+", " ", (triggers.get(guard) or "").lower()).replace(" if not exists", "").strip()
            message = "append-only assessment" if name == "assessments" else "append-only journal"
            expected = (f"create trigger {guard} before {operation} on {table} "
                        f"begin select raise(abort, '{message}'); end")
            _require(sql == expected, f"missing or altered append-only guard {guard}")
    for name, (table, fields) in _INDICES.items():
        index = f"cross_asset_{name}"
        definition = next((row for row in schema if row[0] == "index" and row[1] == index), None)
        actual = tuple(row[2] for row in con.execute(f'PRAGMA index_info("{index}")'))
        _require(definition is not None and definition[2] == f"cross_asset_{table}" and actual == fields,
                 f"missing or altered native query index {index}")
    return columns


def verify_market_database(con: sqlite3.Connection) -> dict:
    """Verify a complete native store without initialization or schema changes.

    Supports the current native schema, optionally alongside legacy tables. The
    caller owns transaction isolation, SQLite integrity checks and SQL progress
    limits. This owner additionally streams at most a million rows/512 MiB of
    payloads. It does not change row_factory, PRAGMAs, user_version or originals.
    """
    try:
        return _verify(con)
    except (KeyError, TypeError, json.JSONDecodeError, OverflowError, sqlite3.Error) as exc:
        raise ValueError(f"Native market recovery verification failed: {exc}") from exc


def _verify(con):
    columns = _schema(con)
    counts, total_bytes = {}, 0

    def rows(table):
        names = _COLUMNS[table].split()
        cursor = con.execute(f"SELECT {','.join(names)} FROM cross_asset_{table}")
        counts[table] = 0
        for values in cursor:
            counts[table] += 1
            _require(sum(counts.values()) <= _MAX_ROWS, "row verification budget exceeded")
            row = dict(zip(names, values, strict=True))
            for field in ("start", "end", "observed_at", "known_at", "received_at", "declared_at", "effective_at"):
                if field in row:
                    _clock(row[field])
            yield row

    def payload(row, field="payload", hashed=True):
        nonlocal total_bytes
        encoded = row[field]
        _require(isinstance(encoded, str), "original payload is not text")
        size = len(encoded.encode())
        total_bytes += size
        _require(size <= 32 * 1024 * 1024 and total_bytes <= _MAX_BYTES, "payload byte verification budget exceeded")
        result = json.loads(encoded)
        _require(isinstance(result, dict) and canonical_json(result) == encoded, "noncanonical original payload")
        if hashed:
            _digest(row["hash"])
            _require(content_hash(result) == row["hash"], "original payload hash mismatch")
        return result

    imports = {}
    for row in rows("imports"):
        _digest(row["hash"])
        summary = payload(row, "summary", False)
        _require(summary.get("schema") == "market-import-result.v1" and summary.get("hash") == row["hash"]
                 and summary.get("received_at") == row["received_at"] and summary.get("idempotent") is False,
                 "import summary identity or receipt mismatch")
        for field in ("series", "bars", "actions"):
            _require(type(summary.get(field)) is int and summary[field] >= 0, "invalid import summary count")
        imports[row["hash"]] = row["received_at"]
    expected_components, registry_ids = {}, set()
    for row in rows("registries"):
        original = payload(row)
        registry = validate_registry(original)
        clocks = [item["known_at"] for kind in ("listings", "aliases", "memberships") for item in registry[kind]]
        _require(row["declared_at"] == max(clocks, default=0), "registry declared clock mismatch")
        _require(row["known_at"] in {min(clocks, default=0), max(min(clocks, default=0), row["received_at"])},
                 "registry admission clock mismatch")
        registry_ids.add(row["hash"])
        for kind, fields in _KEYS.items():
            for item in original.get(kind, []):
                key = (kind, canonical_json([item[field] for field in fields]))
                digest = content_hash(item)
                _require(key not in expected_components or expected_components[key] == digest, "conflicting registry originals")
                expected_components[key] = digest
                if kind == "listings":
                    stable = {field: item.get(field) for field in ("instrument_id", "native_currency", "venue_mic", "venue_id")}
                    expected_components[("listing_identity", item["id"])] = content_hash(stable)
    components, instruments, listings = {}, {}, {}
    for row in rows("registry_components"):
        item = payload(row)
        key = row["kind"], row["identity"]
        _require(expected_components.get(key) == row["hash"], "registry component differs from original registry")
        components[key] = {"known_at": row["known_at"], "received_at": row["received_at"]}
        if row["kind"] == "instruments":
            instruments[item["id"]] = item
        elif row["kind"] == "listings":
            listings[item["id"]] = item
        if "known_at" in item:
            _require(row["known_at"] in {item["known_at"], max(item["known_at"], row["received_at"])},
                     "registry component clock mismatch")
    _require(components.keys() == expected_components.keys(), "missing registry originals")
    calendars = {}
    for row in rows("calendars"):
        original = payload(row)
        calendar = validate_calendar(original)
        _require((row["id"], row["version"], row["declared_at"]) == (calendar["id"], calendar["version"], calendar["known_at"]),
                 "calendar indexed dimensions differ from original")
        _require(row["known_at"] in {row["declared_at"], max(row["declared_at"], row["received_at"])}, "calendar clock mismatch")
        calendars[(row["id"], row["version"])] = {"hash": row["hash"], "calendar": calendar}
    admissions = set()
    for row in rows("admissions"):
        _require(imports.get(row["import_hash"]) == row["received_at"], "admission import or receipt mismatch")
        if row["kind"].startswith("registry:"):
            _require((row["kind"].removeprefix("registry:"), row["identity"]) in components, "orphan registry admission")
        else:
            _require(row["kind"] == "calendar" and tuple(json.loads(row["identity"])) in calendars, "orphan calendar admission")
        admissions.add((row["kind"], row["identity"]))
    _require(all(("registry:" + kind, key) in admissions for kind, key in components if kind != "listing_identity"),
             "registry component admission is missing")
    _require(all(("calendar", canonical_json(list(key))) in admissions for key in calendars), "calendar admission is missing")
    series = {}
    for row in rows("series"):
        descriptor = payload(row)
        validate_series({"schema": "market-series.v1", "descriptor": descriptor, "bars": []})
        for field in ("id", "instrument_id", "listing_id", "provider", "native_currency", "unit", "price_basis", "data_mode",
                      "calendar_id", "calendar_version", "interval"):
            _require(row[field] == descriptor[field], f"series indexed {field} differs from original")
        _require(row["instrument_id"] in instruments and row["registry_hash"] in registry_ids, "orphan series registry")
        _require((row["calendar_id"], row["calendar_version"]) in calendars, "orphan series calendar")
        if row["listing_id"] is not None:
            listing = listings.get(row["listing_id"], {})
            _require(all(listing.get(field) == row[field] for field in ("instrument_id", "native_currency", "calendar_id")),
                     "series listing dimensions disagree")
        series[row["id"]] = descriptor
    for kind in ("bars", "actions"):
        for row in rows(kind):
            item = payload(row)
            descriptor = series.get(row["series_id"])
            _require(descriptor is not None and imports.get(row["import_hash"]) == row["received_at"], "orphan native observation")
            validate_series({"schema": "market-series.v1", "descriptor": descriptor, "bars": [item] if kind == "bars" else [],
                             "actions": [item] if kind == "actions" else []})
            mapping = {"start": "start", "end": "end", "observed_at": "observed_at"} if kind == "bars" else {
                "action_id": "id", "effective_at": "effective_at", "observed_at": "observed_at"}
            _require(all(row[column] == item[field] for column, field in mapping.items()), "native indexed bounds or identity mismatch")
            known = max(row["observed_at"], row["received_at"]) if descriptor["data_mode"] == "live" else row["observed_at"]
            _require(row["known_at"] == known, "native receipt visibility mismatch")
    admitted_series = set()
    for row in rows("series_imports"):
        _digest(row["hash"])
        _require(row["series_id"] in series and imports.get(row["import_hash"]) == row["received_at"], "orphan series admission")
        _require(all(type(row[field]) is int and 0 <= row[field] <= 100000 for field in ("bars", "actions")), "invalid series counts")
        if series[row["series_id"]]["data_mode"] == "live":
            _require(row["known_at"] >= row["received_at"], "live series admission precedes actual receipt")
        admitted_series.add(row["series_id"])
    _require(admitted_series == series.keys(), "series admission is missing")
    assessments = 0
    if "assessments" in columns:
        for identity, key, encoded in con.execute("SELECT id,idempotency_key,payload FROM cross_asset_assessments"):
            assessments += 1
            _require(sum(counts.values()) + assessments <= _MAX_ROWS, "assessment verification row budget exceeded")
            record = payload({"payload": encoded}, hashed=False)
            _clock(record["issued_at"])
            _require(identity == record["assessment_id"] == content_hash({field: value for field, value in record.items() if field != "assessment_id"})
                     and key == record["idempotency_key"] and record["schema"] == "market-assessment.v1", "assessment original hash mismatch")
            frozen = record["protocol"]
            _require(frozen == freeze_market_protocol(frozen["declaration"], frozen_at=frozen["frozen_at"]), "assessment frozen protocol mismatch")
            declaration = frozen["declaration"]
            descriptor = declaration["series"]
            _require(descriptor == series.get(descriptor["id"]), "assessment series lineage mismatch")
            calendar = calendars.get((descriptor["calendar_id"], descriptor["calendar_version"]), {})
            _require(calendar.get("calendar") == declaration["calendar"], "assessment calendar lineage mismatch")
            eligibility = declaration["eligibility"]
            _require(eligibility["start"] <= record["issued_at"] < eligibility["end"], "assessment issuance outside frozen eligibility")
            anchor = record["anchor"]
            row = con.execute("SELECT hash,known_at FROM cross_asset_bars WHERE series_id=? AND start=? AND end=? AND observed_at=?",
                              (descriptor["id"], anchor["start"], anchor["end"], anchor["observed_at"])).fetchone()
            _require(row is not None and row[0] == content_hash(anchor) and row[1] <= record["issued_at"], "assessment anchor mismatch")
            _require(0 <= number(record["probability_up"], "probability_up") <= 1, "invalid assessment probability")
            _digest(record["request_sha256"])
            target = record["target"]
            for field in ("start", "end", "due_at"):
                _clock(target[field])
            _require(target["calendar_id"] == descriptor["calendar_id"] and target["calendar_version"] == descriptor["calendar_version"]
                     and target["start"] < target["end"] and record["issued_at"] < target["end"] <= target["due_at"],
                     "assessment target dimensions or bounds mismatch")
    return {"schema": "market-database-verification.v1", "tables_verified": len(columns), "rows_verified": counts,
            "payload_bytes_verified": total_bytes, "assessments_verified": assessments,
            "bundle_digest_scope": "lineage_reference_without_original_bundle_bytes"}
