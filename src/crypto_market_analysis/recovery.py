"""Explicit local SQLite recovery and checked, storage-independent original replay.

Bundles are checksum inventories, not signed attestations. They preserve a SQLite
backup snapshot and available build metadata; they do not package an old runtime.
Named history datasets include required immutable payloads and pending recovery
records. Their writers must be stopped explicitly; detected changes abort backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from . import __version__
from .evidence import CapturedInputs, canonical_json, content_hash, restore_evidence
from .journal import MAX_RECORD_BYTES, SCHEMA_VERSION
from .models import Candle, Observation

BACKUP_SCHEMA_VERSION = 1
DATABASE_NAME = "observer.sqlite"
_MANIFEST = "manifest.json"
_BUILD_FILES = ("uv.lock", "pyproject.toml")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_DATASET_MAX_FILES = 10_000
_DATASET_MAX_BYTES = 512 * 1024**2
_BASE_COLUMNS = {
    "candles": "id source asset quote timeframe open_time close_time observed_at data_mode fingerprint payload",
    "observations": "id source asset metric event_time observed_at data_mode dimension fingerprint payload",
    "runs": "id source task observed_at payload",
    "events": "id observed_at event_time payload",
    "local_records": "id kind identity observed_at fingerprint payload",
    "assessment_journal": "assessment_id idempotency_key issued_at content_hash record",
    "assessment_outcomes": "sequence assessment_id outcome_key content_hash record",
}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _build_provenance() -> dict:
    package = Path(__file__).parent
    code = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        code.update(path.relative_to(package).as_posix().encode())
        code.update(b"\0")
        code.update(path.read_bytes())
    root = package.parent.parent
    return {"package_version": __version__, "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version, "code_revision": None,
            "code_content_hash": code.hexdigest(),
            "dependency_lock_hash": _file_hash(root / "uv.lock") if (root / "uv.lock").is_file() else None,
            "project_metadata_hash": _file_hash(root / "pyproject.toml") if (root / "pyproject.toml").is_file() else None,
            "runtime_packaged": False}


def _json_object(value: object, name: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a JSON object.")
    try:
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError(f"{name} exceeds the 32 MiB record limit.")
        return json.loads(encoded)
    except (TypeError, RecursionError, OverflowError) as exc:
        raise ValueError(f"Invalid {name} JSON.") from exc


def _evidence_archive(envelope: dict) -> dict:
    if "encoding" in envelope and envelope.get("encoding") != "zlib+base64":
        raise ValueError("Unsupported evidence archive encoding.")
    if envelope.get("encoding") and envelope.get("schema_version") != 1:
        raise ValueError("Unsupported evidence archive schema version.")
    evidence = restore_evidence(envelope)
    if envelope.get("encoding"):
        for key, rows in (("candles_count", "candles"), ("observations_count", "observations")):
            if envelope.get(key) != len(evidence.get(rows, [])):
                raise ValueError("Evidence archive row count mismatch.")
    return evidence


def _original(record: dict) -> tuple[dict, dict]:
    record = _json_object(record, "Assessment record")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported assessment journal schema version.")
    try:
        payload, envelope = record["payload"], record["evidence"]
        digest = content_hash({"payload": payload, "evidence": envelope})
        identity = "assessment_" + content_hash([record["idempotency_key"], digest])
        if digest != record["content_hash"] or identity != record["assessment_id"]:
            raise ValueError("Original assessment content hash or identity mismatch.")
        if type(record["issued_at"]) is not int or record["issued_at"] < 0:
            raise ValueError("Invalid original issuance timestamp.")
        provenance = record["provenance"]
        for key in ("config", "rules"):
            if (provenance[key] != payload.get(key, {})
                    or provenance[key + "_hash"] != content_hash(provenance[key])):
                raise ValueError("Original assessment provenance hash mismatch.")
        evidence = _evidence_archive(envelope)
        if payload.get("kind") == "accumulation_assessment":
            base = {key: value for key, value in evidence.items()
                    if key not in ("plan_candle_versions", "plan_uses_captured_candles")}
            if (payload["input_reference"]["content_hash"] != content_hash(base)
                    or payload["input_reference"]["configuration_hash"] != content_hash(base["configuration"])):
                raise ValueError("Original captured input content or configuration hash mismatch.")
        return record, evidence
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete original assessment envelope.") from exc


def replay_assessment(record: dict) -> dict:
    """Check and recompute an original assessment at its frozen clocks.

    No store, clock sampling, collection, outcome resolution or writes are used.
    Unsupported versions and any mismatch raise ``ValueError``. Generic journal
    records remain backup-verifiable but only accumulation assessments replay.
    """
    from .context import market_context
    from .plans import RULE_VERSION, compare_plans, validate_config
    from .purchase_windows import analyze_windows

    record, evidence = _original(record)
    payload = record["payload"]
    if payload.get("kind") != "accumulation_assessment":
        raise ValueError("Only original accumulation assessments support checked replay.")
    rules = payload.get("rules", {})
    if (rules.get("context") != 1 or rules.get("purchase_windows") != 1
            or set(rules) - {"context", "purchase_windows", "outcome_protocol"}
            or ("outcome_protocol" in rules and rules["outcome_protocol"] != 1)):
        raise ValueError("Unsupported frozen assessment rule version.")
    protocol = payload.get("outcome_protocol")
    if protocol is not None and (protocol.get("schema_version") != 1
                                 or protocol.get("endpoint_convention") != "hourly_after_deadline_v1"):
        raise ValueError("Unsupported frozen outcome protocol.")
    plan = payload.get("plan")
    if plan is not None and (plan.get("schema_version") != 1 or plan.get("rule_version") != RULE_VERSION):
        raise ValueError("Unsupported frozen plan rule version; the original remains unchanged.")
    try:
        base = {key: value for key, value in evidence.items()
                if key not in ("plan_candle_versions", "plan_uses_captured_candles")}
        captured = CapturedInputs(**{**base, "candles": tuple(Candle(**item) for item in base["candles"]),
                                    "observations": tuple(Observation(**item) for item in base["observations"])})
        reference = payload["input_reference"]
        if reference.get("rule_version") != "captured-inputs-1" or reference.get("schema_version") != "2.0":
            raise ValueError("Unsupported frozen captured-input schema or rule version.")
        # The archive adds plan vintages after creating the original capture hash.
        # These hashes describe different evidence sets and must never be equated.
        if reference["content_hash"] != content_hash(base):
            raise ValueError("Original captured input content hash mismatch.")
        expected = captured.reference
        if any(value != expected.get(key) for key, value in reference.items()):
            raise ValueError("Original captured input reference mismatch.")
        if payload["config"] != captured.configuration:
            raise ValueError("Original captured configuration mismatch.")
        if captured.as_of > record["issued_at"]:
            raise ValueError("Original capture cannot follow issuance.")
        if content_hash(payload["request"]) != payload["request_hash"]:
            raise ValueError("Original request hash mismatch.")
        if payload["historical_calculation"] != (captured.as_of != record["issued_at"]):
            raise ValueError("Original historical calculation flag mismatch.")
        context = market_context(list(captured.candles), captured.as_of)
        windows = analyze_windows(list(captured.candles), captured.as_of, issued_at=record["issued_at"],
                                  max_price_age=captured.configuration["market_interval"] * 2)
        if content_hash(context) != content_hash(payload["context"]):
            raise ValueError("Original context calculation mismatch under the supported rule.")
        if content_hash(windows) != content_hash(payload["windows"]):
            raise ValueError("Original purchase-window calculation mismatch under the supported rule.")
        replayed_plan = None
        if plan is not None:
            config = validate_config(plan["config"])
            extras = [Candle(**item) for item in evidence["plan_candle_versions"]]
            # Early archives contained all vintages; current archives contain only
            # extras. Keep latest captured vintages last for equal-availability ties.
            rows = extras + list(captured.candles) if evidence.get("plan_uses_captured_candles") else extras
            rows.sort(key=lambda row: (row.open_time, row.observed_at))
            plan_reference = plan["input_reference"]
            if (plan_reference["content_hash"] != content_hash(rows)
                    or plan_reference["configuration_hash"] != content_hash(config)):
                raise ValueError("Original plan input hash mismatch.")
            for key in ("source", "asset", "quote", "data_mode", "as_of", "watermark", "dataset_lineage"):
                if key in plan_reference and plan_reference[key] != reference.get(key):
                    raise ValueError("Original plan input identity mismatch.")
            if ("available_at" in plan_reference
                    and plan_reference["available_at"] != max((row.observed_at for row in rows), default=0)):
                raise ValueError("Original plan availability mismatch.")
            if config["start_time"] > captured.as_of:
                # One canonical implementation owns the frozen future-plan shape.
                from .application import _future_plan

                replayed_plan = _future_plan(config, captured.as_of)
            else:
                replayed_plan = compare_plans(config, rows, captured.as_of,
                                             validation=plan.get("validation_mode", "retrospective"))
            replayed_plan["input_reference"] = plan_reference
            if content_hash(replayed_plan) != content_hash(plan):
                raise ValueError("Original plan calculation mismatch under the supported rule.")
        current = _build_provenance()
        return {"schema_version": 1, "status": "verified", "assessment_id": record["assessment_id"],
                "issued_at": record["issued_at"], "as_of": captured.as_of,
                "original_content_hash": record["content_hash"], "input_reference": reference,
                "rules": rules, "context": context, "windows": windows, "plan": replayed_plan,
                "provenance": {"original": record["provenance"], "current": current,
                               "same_code": record["provenance"].get("code_content_hash") == current["code_content_hash"],
                               "same_dependency_lock": record["provenance"].get("dependency_lock_hash") == current["dependency_lock_hash"]},
                "scope": "original_calculations_only_no_future_outcomes"}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete or unsupported frozen assessment evidence.") from exc


def _database_check(path: Path, *, named_dataset: bool = False) -> dict:
    # Bundles contain a complete standalone database; immutable reads cannot create
    # WAL/SHM files or silently consult sidecars that are outside the hash inventory.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
        con.execute("PRAGMA trusted_schema=OFF")
        if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed.")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("SQLite foreign key check failed.")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != 2:
            raise ValueError(f"Unsupported SQLite store schema version: {version}.")
        schema = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                             "ORDER BY type,name").fetchall()
        tables = {row[1] for row in schema if row[0] == "table"}
        if any(name.startswith("history_") for name in tables) and not named_dataset:
            raise ValueError("Named history dataset requires its dataset.json identity and payload lineage.")
        if named_dataset and not {"history_captures", "history_repairs", "history_attempts"} <= tables:
            raise ValueError("Incomplete named history dataset schema.")
        if set(_BASE_COLUMNS) - {"assessment_journal", "assessment_outcomes"} - tables:
            raise ValueError("SQLite database is missing required market store tables.")
        if bool("assessment_journal" in tables) != bool("assessment_outcomes" in tables):
            raise ValueError("Incomplete assessment journal schema.")
        for table, names in _BASE_COLUMNS.items():
            if table in tables:
                columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
                if set(names.split()) - columns:
                    raise ValueError(f"Unsupported SQLite table schema: {table}.")
        originals, outcomes = {}, 0
        if "assessment_journal" in tables:
            triggers = {row[1] for row in schema if row[0] == "trigger"}
            required = {f"assessment_{table}_no_{action}" for table in ("journal", "outcomes")
                        for action in ("update", "delete")}
            if required - triggers:
                raise ValueError("Assessment journal immutability guards are missing.")
            for identity, key, issued, digest, encoded in con.execute(
                    "SELECT assessment_id,idempotency_key,issued_at,content_hash,record FROM assessment_journal"):
                record, _ = _original(json.loads(encoded))
                if (identity, key, issued, digest) != tuple(record[field] for field in (
                        "assessment_id", "idempotency_key", "issued_at", "content_hash")):
                    raise ValueError("Assessment journal columns do not match the original record.")
                originals[identity] = issued
            for identity, key, digest, encoded in con.execute(
                    "SELECT assessment_id,outcome_key,content_hash,record FROM assessment_outcomes"):
                record = _json_object(json.loads(encoded), "Outcome record")
                if (record.get("schema_version") != SCHEMA_VERSION or identity not in originals
                        or (identity, key, digest) != tuple(record.get(field) for field in (
                            "assessment_id", "outcome_key", "content_hash"))
                        or digest != content_hash(record.get("payload"))
                        or type(record.get("observed_at")) is not int
                        or record["observed_at"] < originals[identity]):
                    raise ValueError("Assessment outcome content or identity mismatch.")
                outcomes += 1
        return {"user_version": version, "schema_hash": content_hash(schema),
                "integrity": "ok", "originals_verified": len(originals), "outcomes_verified": outcomes}


def _dataset_identity(path: Path) -> dict:
    from .history_dataset import DatasetConfig

    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("Invalid or oversized named dataset identity.")
    identity = _json_object(json.loads(path.read_text(encoding="utf-8")), "Dataset identity")
    try:
        DatasetConfig(**identity)
    except TypeError as exc:
        raise ValueError("Unsupported named dataset configuration.") from exc
    return identity


def _dataset_scan(directory: Path, max_files: int, max_bytes: int) -> dict:
    """Bound flat dataset evidence discovery without reading arbitrary archive files."""
    result, total = {}, 0
    for path in directory.iterdir():
        if path.name != "dataset.json" and not (path.name.endswith(".payload") or path.name.startswith("pending-")):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Named dataset evidence must contain regular files without symlinks.")
        if path.name.startswith("pending-") and not re.fullmatch(r"pending-[0-9a-f]{64}\.json", path.name):
            raise ValueError("Named dataset has unfinished staging files; stop and recover its writer before backup.")
        if path.name.endswith(".payload") and not _DIGEST.fullmatch(path.stem):
            raise ValueError("Named dataset payload names must be content-addressed SHA256 values.")
        stat = path.stat()
        total += stat.st_size
        result[path.name] = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if len(result) > max_files or total > max_bytes:
            raise ValueError("Named dataset evidence exceeds the configured file or byte bound.")
    return result


def _dataset_required(database: Path, directory: Path, pending_names: list[str]) -> set[str]:
    """Follow only snapshot capture and explicit pending references to payloads."""
    required = {"dataset.json"}

    def consume(record: dict) -> None:
        if not _DIGEST.fullmatch(record.get("id", "")):
            raise ValueError("Invalid named dataset capture identity.")
        for key in ("normalized_sha256", "raw_sha256"):
            digest = record.get(key)
            if digest is None and key == "raw_sha256":
                continue
            if type(digest) is not str or not _DIGEST.fullmatch(digest):
                raise ValueError("Invalid named dataset payload reference.")
            required.add(digest + ".payload")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
        for identity, encoded in con.execute("SELECT id,payload FROM history_captures"):
            record = _json_object(json.loads(encoded), "Dataset capture")
            if identity != record.get("id"):
                raise ValueError("Dataset capture columns disagree with their lineage record.")
            consume(record)
        if con.execute("SELECT 1 FROM candles WHERE source!='kraken' OR data_mode!='import' LIMIT 1").fetchone():
            raise ValueError("Named dataset contains a foreign source or mode.")
    for name in pending_names:
        path = directory / name
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("Named dataset pending record exceeds the 32 MiB bound.")
        record = _json_object(json.loads(path.read_text(encoding="utf-8")), "Pending dataset capture")
        consume(record)
        if name != "pending-" + record["id"] + ".json":
            raise ValueError("Pending dataset capture filename disagrees with its identity.")
        required.add(name)
    return required


def _destination(path: Path) -> Path:
    # Resolve the parent only: resolving a destination symlink would hide it.
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise ValueError("Recovery destination must be absent or an empty directory; nothing is overwritten.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _publish(stage: Path, destination: Path) -> None:
    _destination(destination)
    for path in stage.iterdir():
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    # Same-filesystem directory rename publishes all checked files in one step.
    # A concurrent nonempty target makes rename fail without modifying that target.
    os.rename(stage, destination)


def create_backup(database: Path, destination: Path, *, quiescent_dataset: bool = False,
                  max_dataset_files: int = _DATASET_MAX_FILES,
                  max_dataset_bytes: int = _DATASET_MAX_BYTES) -> dict:
    """Back up an existing store via SQLite's online API, including committed WAL.

    The source is opened read-only and never initialized or migrated. Verification
    completes in a private sibling staging directory before atomic publication.
    Named dataset writers must be stopped and ``quiescent_dataset=True`` supplied;
    observed database or evidence-file changes abort rather than publishing a
    cross-file snapshot. Defaults bound dataset evidence to 10,000 files/512 MiB.
    """
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        raise ValueError("Backup source must be an existing SQLite database.")
    named = (database.parent / "dataset.json").exists()
    if any(type(value) is not int or value <= 0 for value in (max_dataset_files, max_dataset_bytes)):
        raise ValueError("Dataset backup file and byte bounds must be positive integers.")
    if named and quiescent_dataset is not True:
        raise ValueError("Stop the named dataset writer and explicitly set quiescent_dataset=True before backup.")
    identity = _dataset_identity(database.parent / "dataset.json") if named else None
    if named and (database.name != DATABASE_NAME or Path(identity["directory"]).resolve() != database.parent):
        raise ValueError("Named dataset database path does not match its identity.")
    before = _dataset_scan(database.parent, max_dataset_files, max_dataset_bytes) if named else None
    destination = _destination(destination)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent))
    try:
        target = stage / DATABASE_NAME
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)) as source:
            version = source.execute("PRAGMA data_version").fetchone()[0]
            with closing(sqlite3.connect(target)) as snapshot:
                source.backup(snapshot, pages=256, sleep=0.01)
                # Backups from WAL sources may retain WAL mode. Make a portable
                # single-file snapshot before hashing, without touching the source.
                snapshot.execute("PRAGMA journal_mode=DELETE")
            check = _database_check(target, named_dataset=named)
            if named:
                pending = sorted(name for name in before if name.startswith("pending-"))
                required = _dataset_required(target, database.parent, pending)
                if not required <= before.keys():
                    raise ValueError("Named dataset is missing required payload lineage files.")
                for name in sorted(required):
                    shutil.copyfile(database.parent / name, stage / name)
                after = _dataset_scan(database.parent, max_dataset_files, max_dataset_bytes)
                if (before != after or source.execute("PRAGMA data_version").fetchone()[0] != version):
                    raise ValueError("Named dataset changed during backup; stop its writer and retry.")
        build = _build_provenance()
        root = Path(__file__).parent.parent.parent
        for name in _BUILD_FILES:
            if (root / name).is_file():
                shutil.copyfile(root / name, stage / name)
        manifest = {"schema_version": BACKUP_SCHEMA_VERSION, "kind": "crypto_market_analysis_sqlite_backup",
                    "created_at": int(time.time()), "database": DATABASE_NAME,
                    "snapshot_method": "sqlite_online_backup", "database_verification": check,
                    "build": build,
                    "files": {path.name: {"sha256": _file_hash(path), "bytes": path.stat().st_size}
                              for path in sorted(stage.iterdir())}}
        if named:
            manifest["dataset"] = {"identity": identity, "original_identity": identity,
                                   "directory_rebased": False, "writer_precondition": "stopped_explicitly_confirmed",
                                   "max_files": max_dataset_files, "max_bytes": max_dataset_bytes,
                                   "pending_records": pending, "payload_selection": "snapshot_captures_and_pending_only"}
        (stage / _MANIFEST).write_text(canonical_json(manifest), encoding="utf-8")
        result = verify_backup(stage)
        _publish(stage, destination)
        return {**result, "bundle": str(destination), "database": str(destination / DATABASE_NAME)}
    except sqlite3.Error as exc:
        raise ValueError(f"SQLite backup failed: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_backup(bundle: Path) -> dict:
    """Read-only hash, SQLite schema/integrity and original journal verification.

    This verifies preservation of originals, including unknown calculation rules;
    ``replay_assessment`` separately verifies supported calculation outputs.
    """
    bundle = Path(bundle).expanduser().resolve()
    try:
        manifest_path = bundle / _MANIFEST
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 2_000_000:
            raise ValueError("Invalid or oversized backup manifest.")
        manifest = _json_object(json.loads(manifest_path.read_text(encoding="utf-8")), "Backup manifest")
        if (manifest.get("schema_version") != BACKUP_SCHEMA_VERSION
                or manifest.get("kind") != "crypto_market_analysis_sqlite_backup"
                or manifest.get("database") != DATABASE_NAME):
            raise ValueError("Unsupported backup manifest schema.")
        files = manifest["files"]
        if type(files) is not dict:
            raise ValueError("Unsupported backup file inventory.")
        named = "dataset" in manifest
        allowed = {DATABASE_NAME, *_BUILD_FILES}
        if named:
            allowed.add("dataset.json")
            allowed.update(name for name in files if re.fullmatch(r"[0-9a-f]{64}\.payload|pending-[0-9a-f]{64}\.json", name))
        if DATABASE_NAME not in files or set(files) - allowed:
            raise ValueError("Unsupported backup file inventory.")
        if {path.name for path in bundle.iterdir()} != {*files, _MANIFEST}:
            raise ValueError("Backup contains missing or unlisted files, including SQLite sidecars.")
        for name, info in files.items():
            path = bundle / name
            if (path.is_symlink() or not path.is_file() or path.stat().st_size != info["bytes"]
                    or _file_hash(path) != info["sha256"]):
                raise ValueError(f"Backup file checksum or size mismatch: {name}.")
        for name, key in (("uv.lock", "dependency_lock_hash"), ("pyproject.toml", "project_metadata_hash")):
            if (manifest["build"].get(key) is not None
                    and (name not in files or files[name]["sha256"] != manifest["build"][key])):
                raise ValueError("Backup build provenance checksum mismatch.")
        check = _database_check(bundle / DATABASE_NAME, named_dataset=named)
        if named:
            dataset = manifest["dataset"]
            if any(type(dataset[key]) is not int or dataset[key] <= 0 for key in ("max_files", "max_bytes")):
                raise ValueError("Invalid named dataset resource bounds.")
            scan = _dataset_scan(bundle, dataset["max_files"], dataset["max_bytes"])
            if (_dataset_identity(bundle / "dataset.json") != dataset["identity"]
                    or sorted(name for name in scan if name.startswith("pending-")) != dataset["pending_records"]):
                raise ValueError("Backup named dataset identity or pending inventory mismatch.")
            required = _dataset_required(bundle / DATABASE_NAME, bundle, dataset["pending_records"])
            if required != set(scan):
                raise ValueError("Backup named dataset payload lineage inventory mismatch.")
            for name in required:
                if name.endswith(".payload") and files[name]["sha256"] != name.removesuffix(".payload"):
                    raise ValueError("Backup named dataset content-addressed payload checksum mismatch.")
        if check != manifest["database_verification"]:
            raise ValueError("Backup SQLite schema or journal verification mismatch.")
        return {"schema_version": BACKUP_SCHEMA_VERSION, "status": "verified", "bundle": str(bundle),
                "database": str(bundle / DATABASE_NAME), "manifest": manifest, "verification": check}
    except (OSError, sqlite3.Error, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError(f"Invalid backup bundle: {exc}") from exc


def restore_backup(bundle: Path, destination: Path) -> dict:
    """Restore into a new/empty directory, publishing only a completely checked copy.

    Existing stores and nonempty directories are never overwritten. The restored
    manifest stays alongside the database as a portable recovery receipt.
    """
    bundle = Path(bundle).expanduser().resolve()
    verified = verify_backup(bundle)
    destination = _destination(destination)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent))
    try:
        for name in (*verified["manifest"]["files"], _MANIFEST):
            shutil.copyfile(bundle / name, stage / name)
        if json.loads((stage / _MANIFEST).read_text(encoding="utf-8")) != verified["manifest"]:
            raise ValueError("Backup manifest changed during restore; no destination was published.")
        if "dataset" in verified["manifest"]:
            manifest = verified["manifest"]
            dataset = manifest["dataset"]
            original_directory = dataset["identity"]["directory"]
            dataset["identity"] = {**dataset["identity"], "directory": str(destination.resolve())}
            dataset["directory_rebased"] = True
            # Match HistoryDataset's exact canonical identity encoding on reopen.
            identity_path = stage / "dataset.json"
            identity_path.write_text(json.dumps(dataset["identity"], sort_keys=True, allow_nan=False,
                                               separators=(",", ":")), encoding="utf-8")
            manifest["files"]["dataset.json"] = {"sha256": _file_hash(identity_path), "bytes": identity_path.stat().st_size}
            manifest["restore"] = {"source_manifest_sha256": _file_hash(bundle / _MANIFEST),
                                   "directory_rebase": {"from": original_directory, "to": str(destination.resolve())}}
            (stage / _MANIFEST).write_text(canonical_json(manifest), encoding="utf-8")
        result = verify_backup(stage)
        _publish(stage, destination)
        return {**result, "status": "restored", "bundle": str(destination),
                "database": str(destination / DATABASE_NAME), "source_bundle": str(bundle)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
