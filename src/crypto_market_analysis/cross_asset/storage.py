"""Append-only native market observations in the authoritative SQLite database.

Opening a reader never creates a database or installs a schema. Original source
clocks and component hashes are retained; live visibility also requires receipt
by this journal. Historical import/demo clocks are explicitly self-declared.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..evidence import canonical_json, content_hash

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_IMPORT_ROWS = 100_000
MAX_READ_ROWS = 100_000
QUERY_SECONDS = 10
MODES = ("live", "import", "demo")

_DDL = """
CREATE TABLE IF NOT EXISTS cross_asset_imports (
 hash TEXT PRIMARY KEY, received_at INTEGER NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cross_asset_registries (
 hash TEXT PRIMARY KEY, declared_at INTEGER NOT NULL, received_at INTEGER NOT NULL,
 known_at INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cross_asset_registry_cutoff ON cross_asset_registries(known_at DESC);
CREATE TABLE IF NOT EXISTS cross_asset_registry_components (
 kind TEXT NOT NULL, identity TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL,
 known_at INTEGER NOT NULL, received_at INTEGER NOT NULL,
 PRIMARY KEY(kind,identity));
CREATE INDEX IF NOT EXISTS cross_asset_component_cutoff ON cross_asset_registry_components(known_at,kind,identity);
CREATE TABLE IF NOT EXISTS cross_asset_admissions (
 kind TEXT NOT NULL, identity TEXT NOT NULL, known_at INTEGER NOT NULL,
 received_at INTEGER NOT NULL, import_hash TEXT NOT NULL,
 PRIMARY KEY(kind,identity,import_hash));
CREATE INDEX IF NOT EXISTS cross_asset_admission_cutoff ON cross_asset_admissions(kind,identity,known_at);
CREATE TABLE IF NOT EXISTS cross_asset_calendars (
 id TEXT NOT NULL, version TEXT NOT NULL, hash TEXT NOT NULL, declared_at INTEGER NOT NULL,
 received_at INTEGER NOT NULL, known_at INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS cross_asset_series (
 id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, listing_id TEXT, provider TEXT NOT NULL,
 native_currency TEXT NOT NULL, unit TEXT NOT NULL, price_basis TEXT NOT NULL,
 data_mode TEXT NOT NULL, calendar_id TEXT NOT NULL, calendar_version TEXT NOT NULL,
 interval TEXT NOT NULL, known_at INTEGER NOT NULL, received_at INTEGER NOT NULL,
 registry_hash TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cross_asset_series_cutoff ON cross_asset_series(data_mode,id,known_at);
CREATE TABLE IF NOT EXISTS cross_asset_bars (
 series_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
 observed_at INTEGER NOT NULL, known_at INTEGER NOT NULL, received_at INTEGER NOT NULL,
 hash TEXT NOT NULL, payload TEXT NOT NULL, import_hash TEXT NOT NULL,
 PRIMARY KEY(series_id,start,end,observed_at));
CREATE INDEX IF NOT EXISTS cross_asset_bar_cutoff ON cross_asset_bars(series_id,start,known_at DESC,observed_at DESC);
CREATE TABLE IF NOT EXISTS cross_asset_actions (
 series_id TEXT NOT NULL, action_id TEXT NOT NULL, effective_at INTEGER NOT NULL,
 observed_at INTEGER NOT NULL, known_at INTEGER NOT NULL, received_at INTEGER NOT NULL,
 hash TEXT NOT NULL, payload TEXT NOT NULL, import_hash TEXT NOT NULL,
 PRIMARY KEY(series_id,action_id,observed_at));
CREATE INDEX IF NOT EXISTS cross_asset_action_cutoff ON cross_asset_actions(series_id,known_at,action_id);
CREATE TABLE IF NOT EXISTS cross_asset_series_imports (
 hash TEXT PRIMARY KEY, series_id TEXT NOT NULL, import_hash TEXT NOT NULL,
 received_at INTEGER NOT NULL, bars INTEGER NOT NULL, actions INTEGER NOT NULL, known_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS cross_asset_series_admission ON cross_asset_series_imports(series_id,known_at);
"""
_TABLES = ("imports", "registries", "registry_components", "admissions", "calendars", "series", "bars", "actions", "series_imports")


def _epoch(value, name):
    if type(value) is not int or not -62135596800 <= value <= 253402300799:
        raise ValueError(f"{name} must be representable integer UTC epoch seconds.")
    return value


def _limit(value, maximum, name):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}.")
    return value


def _mode(value):
    if value not in MODES:
        raise ValueError("data_mode must be live, import or demo.")
    return value


def _bounded(value):
    if len(canonical_json(value).encode()) > MAX_RESULT_BYTES:
        raise ValueError("Cross-asset result exceeds byte budget; narrow the interval.")
    return value


def _registry_clock(document):
    clocks = [row["known_at"] for kind in ("listings", "aliases", "memberships")
              for row in document.get(kind, [])]
    return max(clocks, default=0)


def _registry_components(rows):
    """Union immutable subregistries without dropping omitted old instruments."""
    result = {"schema": "market-registry.v1", "instruments": [], "listings": [], "aliases": []}
    count, size = 0, 0
    for kind, payload in rows:
        count += 1
        size += len(payload.encode())
        if count > 150_000 or size > MAX_RESULT_BYTES:
            raise ValueError("Registry exceeds row/byte budget; use a narrower historical selection.")
        result.setdefault(kind, []).append(json.loads(payload))
    return result


class CrossAssetStore:
    """An explicit writer or a side-effect-free bounded reader of observer.sqlite."""

    def __init__(self, path: Path | str, initialize: bool = False):
        self.path = Path(path)
        self.writable = initialize
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_DDL)
                for name in _TABLES:
                    table = f"cross_asset_{name}"
                    for operation in ("UPDATE", "DELETE"):
                        con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                                    f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only journal'); END")

    @contextmanager
    def _connection(self, *, write=False):
        if write and not self.writable:
            raise ValueError("Cross-asset writes require explicit initialize=True.")
        if not self.path.is_file():
            if write:
                raise ValueError("Authoritative cross-asset database is missing.")
            yield None
            return
        uri = self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro")
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        deadline = time.monotonic() + QUERY_SECONDS
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_asset_imports'").fetchone():
                if write:
                    raise ValueError("Cross-asset schema is missing.")
                yield None
            else:
                yield con
                if write:
                    con.commit()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Cross-asset SQLite read/write failed within its query budget: {exc}") from exc
        finally:
            con.close()

    @contextmanager
    def read_transaction(self):
        """Yield one bounded consistent journal view for explicit export owners."""
        with self._connection() as con:
            yield con

    def import_bundle(self, bundle: dict, *, received_at: int) -> dict:
        """Validate then atomically append components; identical replay is a no-op.

        The receipt clock must be supplied by the importing command's current
        clock, never copied from a payload. It is not an upstream timestamp.
        """
        from .application import validate_bundle

        _epoch(received_at, "received_at")
        if not isinstance(bundle, dict) or set(bundle) != {"schema", "registry", "calendars", "series"}:
            raise ValueError("Expected market-bundle.v1 with registry, calendars and series.")
        if bundle["schema"] != "market-bundle.v1":
            raise ValueError("Unsupported market bundle schema.")
        if len(canonical_json(bundle).encode()) > MAX_BUNDLE_BYTES:
            raise ValueError("Market bundle exceeds byte budget.")
        if not isinstance(bundle["calendars"], list) or not isinstance(bundle["series"], list):
            raise ValueError("Bundle calendars and series must be arrays.")
        if len(bundle["series"]) > 1000 or len(bundle["calendars"]) > 1000:
            raise ValueError("Bundle exceeds 1000-series/calendar fleet budget.")
        # The application owns alias/calendar-grid semantics shared by imports
        # and analyses. Its normalized result is never substituted for originals.
        normalized = validate_bundle(bundle)
        registry = normalized["registry"]
        calendars = normalized["calendars"]
        series = normalized["series"]
        if sum(len(item["bars"]) + len(item.get("actions", [])) for item in series) > MAX_IMPORT_ROWS:
            raise ValueError("Bundle exceeds component row budget.")
        if len({item["descriptor"]["id"] for item in series}) != len(series):
            raise ValueError("Duplicate series in bundle.")
        calendar_map = {(item["id"], str(item["version"])): item for item in calendars}
        if len(calendar_map) != len(calendars):
            raise ValueError("Duplicate calendar version in bundle.")
        instruments = {item["id"]: item for item in registry["instruments"]}
        listings = {item["id"]: item for item in registry["listings"]}
        original_hash = content_hash(bundle)
        registry_hash = content_hash(bundle["registry"])
        summary = {"schema": "market-import-result.v1", "hash": original_hash,
                   "series": len(series), "bars": sum(len(item["bars"]) for item in series),
                   "actions": sum(len(item.get("actions", [])) for item in series),
                   "received_at": received_at, "idempotent": False}
        with self._connection(write=True) as con:
            previous = con.execute("SELECT summary FROM cross_asset_imports WHERE hash=?", (original_hash,)).fetchone()
            if previous:
                return {**json.loads(previous[0]), "idempotent": True}
            # Shared metadata in a mixed bundle also has a self-declared
            # historical admission. Each live observation still requires this
            # import's actual receipt independently below.
            live = bool(series) and all(item["descriptor"]["data_mode"] == "live" for item in series)
            declared = _registry_clock(registry)
            clocks = [row["known_at"] for kind in ("listings", "aliases", "memberships") for row in registry.get(kind, [])]
            admission = min(clocks, default=0)
            registry_known = max(admission, received_at) if live else admission
            # Registry documents retain historical revisions. Existing identities
            # and (logical key, known_at) records may be repeated, never changed.
            logical_fields = {"instruments": ("id",), "listings": ("id", "known_at"),
                              "aliases": ("provider", "symbol", "instrument_id", "listing_id", "effective_from", "known_at"),
                              "memberships": ("universe", "instrument_id", "valid_from", "known_at")}
            for kind, fields in logical_fields.items():
                for component in bundle["registry"].get(kind, []):
                    key = canonical_json([component[field] for field in fields])
                    source_known = component.get("known_at", admission)
                    component_known = max(source_known, received_at) if live else source_known
                    self._insert_immutable(con, "registry_components", "kind=? AND identity=?", (kind, key),
                                           (kind, key, content_hash(component), canonical_json(component), component_known, received_at), content_hash(component))
                    con.execute("INSERT INTO cross_asset_admissions VALUES (?,?,?,?,?)",
                                ("registry:" + kind, key, component_known, received_at, original_hash))
                    if kind == "listings":
                        stable = {field: component.get(field) for field in ("instrument_id", "native_currency", "venue_mic", "venue_id")}
                        self._insert_immutable(con, "registry_components", "kind=? AND identity=?", ("listing_identity", component["id"]),
                                               ("listing_identity", component["id"], content_hash(stable), canonical_json(stable), component_known, received_at), content_hash(stable))
            if con.execute("SELECT COUNT(*) FROM cross_asset_registry_components WHERE kind='instruments'").fetchone()[0] > 1000:
                raise ValueError("Cumulative registry exceeds 1000-instrument fleet budget.")
            con.execute("INSERT OR IGNORE INTO cross_asset_registries VALUES (?,?,?,?,?)",
                        (registry_hash, declared, received_at, registry_known, canonical_json(bundle["registry"])))
            for original, calendar in zip(bundle["calendars"], calendars, strict=True):
                key = (calendar["id"], str(calendar["version"]))
                self._insert_immutable(con, "calendars", "id=? AND version=?", key,
                                       (key[0], key[1], content_hash(original), calendar["known_at"], received_at,
                                        max(calendar["known_at"], received_at) if live else calendar["known_at"],
                                        canonical_json(original)), content_hash(original))
                con.execute("INSERT INTO cross_asset_admissions VALUES (?,?,?,?,?)",
                            ("calendar", canonical_json(list(key)), max(calendar["known_at"], received_at) if live else calendar["known_at"], received_at, original_hash))
            for original, item in zip(bundle["series"], series, strict=True):
                descriptor = item["descriptor"]
                if len(canonical_json(original["descriptor"]).encode()) > 64 * 1024:
                    raise ValueError("Series descriptor exceeds byte budget.")
                for field in ("id", "instrument_id", "listing_id", "provider", "provider_symbol", "unit", "calendar_id", "calendar_version"):
                    if descriptor[field] is not None and len(descriptor[field]) > 256:
                        raise ValueError("Series identity exceeds 256-character budget.")
                sid = descriptor["id"]
                if descriptor["instrument_id"] not in instruments:
                    raise ValueError("Series references an unknown instrument.")
                listing = listings.get(descriptor["listing_id"])
                if descriptor["listing_id"] is not None and (listing is None or
                        listing["instrument_id"] != descriptor["instrument_id"] or
                        listing["native_currency"] != descriptor["native_currency"] or
                        listing["calendar_id"] != descriptor["calendar_id"]):
                    raise ValueError("Series listing/native dimensions disagree with registry.")
                key = (descriptor["calendar_id"], str(descriptor["calendar_version"]))
                calendar = con.execute("SELECT * FROM cross_asset_calendars WHERE id=? AND version=?", key).fetchone()
                if calendar is None:
                    raise ValueError("Series references an unknown calendar version.")
                clocks = [bar["observed_at"] for bar in item["bars"]]
                clocks.extend(action["observed_at"] for action in item.get("actions", []))
                declared = min(clocks, default=received_at)
                known = max(declared, received_at) if descriptor["data_mode"] == "live" else declared
                source_calendar_known = calendar_map[key]["known_at"]
                if descriptor["data_mode"] == "live":
                    source_calendar_known = max(source_calendar_known, received_at)
                known = max(known, source_calendar_known, registry_known)
                desc_hash = content_hash(original["descriptor"])
                dimensions = tuple(descriptor[name] for name in (
                    "id", "instrument_id", "listing_id", "provider", "native_currency", "unit",
                    "price_basis", "data_mode", "calendar_id"))
                self._insert_immutable(con, "series", "id=?", (sid,),
                                       (*dimensions, key[1], descriptor["interval"], known, received_at,
                                        registry_hash, desc_hash, canonical_json(original["descriptor"])), desc_hash)
                original_bars = {(bar["start"], bar["end"], bar["observed_at"]): bar for bar in original["bars"]}
                original_actions = {(action["id"], action["observed_at"]): action for action in original.get("actions", [])}
                if any(len(action["id"]) > 256 for action in original.get("actions", [])):
                    raise ValueError("Action identity exceeds 256-character budget.")
                for records in (original["bars"], original.get("actions", [])):
                    for component in records:
                        if len(canonical_json(component).encode()) > 16 * 1024:
                            raise ValueError("Native component exceeds byte budget.")
                for validated_bar in item["bars"]:
                    bar = original_bars[(validated_bar["start"], validated_bar["end"], validated_bar["observed_at"])]
                    self._append_bar(con, descriptor, bar, received_at, original_hash)
                for validated_action in item.get("actions", []):
                    action = original_actions[(validated_action["id"], validated_action["observed_at"])]
                    previous_kind = con.execute("SELECT json_extract(payload,'$.kind') FROM cross_asset_actions WHERE series_id=? AND action_id=? LIMIT 1", (sid, action["id"])).fetchone()
                    if previous_kind and previous_kind[0] != action["kind"]:
                        raise ValueError("An immutable action identity cannot change kind.")
                    action_known = max(action["observed_at"], received_at) if descriptor["data_mode"] == "live" else action["observed_at"]
                    self._insert_immutable(con, "actions", "series_id=? AND action_id=? AND observed_at=?",
                                           (sid, action["id"], action["observed_at"]),
                                           (sid, action["id"], action["effective_at"], action["observed_at"],
                                            action_known, received_at, content_hash(action), canonical_json(action), original_hash),
                                           content_hash(action))
                con.execute("INSERT OR IGNORE INTO cross_asset_series_imports VALUES (?,?,?,?,?,?,?)",
                            (content_hash(original), sid, original_hash, received_at, len(item["bars"]), len(item.get("actions", [])), known))
            con.execute("INSERT INTO cross_asset_imports VALUES (?,?,?)", (original_hash, received_at, canonical_json(summary)))
        return summary

    @staticmethod
    def _insert_immutable(con, table, where, keys, values, fingerprint):
        previous = con.execute(f"SELECT hash FROM cross_asset_{table} WHERE {where}", keys).fetchone()
        if previous:
            if previous["hash"] != fingerprint:
                raise ValueError(f"Conflicting immutable {table} identity or vintage.")
            return
        con.execute(f"INSERT INTO cross_asset_{table} VALUES ({','.join('?' for _ in values)})", values)

    def _append_bar(self, con, descriptor, bar, received_at, import_hash):
        sid = descriptor["id"]
        overlap = con.execute("SELECT 1 FROM cross_asset_bars WHERE series_id=? AND start<? AND end>? "
                              "AND NOT (start=? AND end=?) LIMIT 1",
                              (sid, bar["end"], bar["start"], bar["start"], bar["end"])).fetchone()
        if overlap:
            raise ValueError("Conflicting overlapping native intervals.")
        known = max(bar["observed_at"], received_at) if descriptor["data_mode"] == "live" else bar["observed_at"]
        self._insert_immutable(con, "bars", "series_id=? AND start=? AND end=? AND observed_at=?",
                               (sid, bar["start"], bar["end"], bar["observed_at"]),
                               (sid, bar["start"], bar["end"], bar["observed_at"], known, received_at,
                                content_hash(bar), canonical_json(bar), import_hash), content_hash(bar))

    def registry(self, as_of: int) -> dict | None:
        _epoch(as_of, "as_of")
        with self._connection() as con:
            if con is None or not con.execute("SELECT 1 FROM cross_asset_admissions WHERE kind LIKE 'registry:%' AND known_at<=? LIMIT 1", (as_of,)).fetchone():
                return None
            rows = con.execute("SELECT kind,payload FROM cross_asset_registry_components c WHERE kind<>'listing_identity' AND EXISTS "
                               "(SELECT 1 FROM cross_asset_admissions a WHERE a.kind='registry:'||c.kind AND a.identity=c.identity AND a.known_at<=?) "
                               "ORDER BY kind,identity LIMIT 150001", (as_of,))
            return _registry_components(rows)

    def calendar(self, calendar_id: str, version: str | None = None, *, as_of: int) -> dict | None:
        """Return the pinned sourced schedule visible at the requested cutoff."""
        _epoch(as_of, "as_of")
        if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 256:
            raise ValueError("calendar_id must be a bounded nonempty string.")
        if version is not None and (not isinstance(version, str) or not version or len(version) > 256):
            raise ValueError("version must be a bounded nonempty string.")
        with self._connection() as con:
            conditions, values = ["id=?", "EXISTS (SELECT 1 FROM cross_asset_admissions a WHERE a.kind='calendar' "
                                   "AND a.identity=json_array(c.id,c.version) AND a.known_at<=?)"], [calendar_id, as_of]
            if version is not None:
                conditions.append("version=?")
                values.append(version)
            row = con.execute("SELECT payload FROM cross_asset_calendars c WHERE " + " AND ".join(conditions) + " ORDER BY declared_at DESC,version DESC LIMIT 1", values).fetchone() if con else None
            return _bounded(json.loads(row[0])) if row else None

    def list_series(self, *, as_of: int, data_mode: str, limit: int = 100, cursor: str | None = None) -> dict:
        _epoch(as_of, "as_of")
        _mode(data_mode)
        _limit(limit, 1000, "limit")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
            raise ValueError("cursor must be a series ID of at most 256 characters.")
        with self._connection() as con:
            rows = con.execute("SELECT id,payload FROM cross_asset_series s WHERE data_mode=? AND EXISTS "
                               "(SELECT 1 FROM cross_asset_series_imports i WHERE i.series_id=s.id AND i.known_at<=?) AND id>? ORDER BY id LIMIT ?",
                               (data_mode, as_of, cursor or "", limit + 1)).fetchall() if con else []
            return _bounded({"schema": "market-series-list.v1", "series": [json.loads(row["payload"]) for row in rows[:limit]],
                             "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})

    def read_series(self, series_id: str, *, as_of: int, data_mode: str, start: int | None = None,
                    end: int | None = None, max_rows: int = 10_000) -> dict | None:
        _epoch(as_of, "as_of")
        _mode(data_mode)
        _limit(max_rows, MAX_READ_ROWS, "max_rows")
        if not isinstance(series_id, str) or not series_id or len(series_id) > 256:
            raise ValueError("series_id must be a nonempty bounded string.")
        for name, value in (("start", start), ("end", end)):
            if value is not None:
                _epoch(value, name)
        if start is not None and end is not None and start >= end:
            raise ValueError("start must precede end.")
        with self._connection() as con:
            row = con.execute("SELECT * FROM cross_asset_series s WHERE id=? AND data_mode=? AND EXISTS "
                              "(SELECT 1 FROM cross_asset_series_imports i WHERE i.series_id=s.id AND i.known_at<=?)",
                              (series_id, data_mode, as_of)).fetchone() if con else None
            if row is None:
                return None
            conditions, values = ["series_id=?", "known_at<=?"], [series_id, as_of]
            if start is not None:
                conditions.append("start>=?")
                values.append(start)
            if end is not None:
                conditions.append("end<=?")
                values.append(end)
            sql = ("WITH versions AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY start,end ORDER BY known_at DESC,observed_at DESC) AS rank "
                   f"FROM cross_asset_bars WHERE {' AND '.join(conditions)}) SELECT * FROM versions WHERE rank=1 ORDER BY start,end LIMIT ?")
            bars = con.execute(sql, (*values, max_rows + 1)).fetchall()
            actions = con.execute("WITH versions AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY known_at DESC,observed_at DESC) AS rank "
                                  "FROM cross_asset_actions WHERE series_id=? AND known_at<=?) SELECT * FROM versions WHERE rank=1 ORDER BY effective_at,action_id LIMIT ?",
                                  (series_id, as_of, max_rows + 1)).fetchall()
            if len(bars) + len(actions) > max_rows:
                raise ValueError("Cross-asset result exceeds row budget; narrow the interval.")
            def receipt(item):
                return {"hash": item["hash"], "declared_observed_at": item["observed_at"],
                                                "received_at": item["received_at"], "effective_known_at": item["known_at"]}
            return _bounded({"schema": "market-series.v1", "descriptor": json.loads(row["payload"]),
                             "bars": [json.loads(item["payload"]) for item in bars],
                             "actions": [json.loads(item["payload"]) for item in actions],
                             "storage_provenance": {"descriptor_hash": row["hash"], "registry_hash": row["registry_hash"],
                                            "receipt_evidence": "locally_witnessed" if data_mode == "live" else "self_declared_history",
                                            "bars": [receipt(item) for item in bars], "actions": [receipt(item) for item in actions]}})
