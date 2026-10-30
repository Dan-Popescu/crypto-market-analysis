"""Bounded read compositions shared by independent CLI and MCP transports."""

from __future__ import annotations

import json


def request_fields(request, allowed, required=()):
    if not isinstance(request, dict) or set(request) - set(allowed) or set(required) - set(request):
        raise ValueError("Market request has missing or unknown fields.")
    return dict(request)


def market_snapshot_path(settings, name):
    import re

    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Market snapshot name must contain 1..64 safe letters, digits, underscores or hyphens.")
    root = settings.data_dir.resolve()
    directory = root / "market_snapshots" / name
    if any(path.is_symlink() for path in (settings.data_dir, root / "market_snapshots", directory)):
        raise ValueError("Market snapshot paths cannot be symbolic links.")
    return directory


def market_snapshot_status(settings, name):
    directory = market_snapshot_path(settings, name)
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return {"status": "missing", "snapshot_name": name, "read_only": True, "integrity_verified": False}
    if manifest.is_symlink() or manifest.stat().st_size > 2_000_000:
        raise ValueError("Market snapshot manifest must be a bounded regular file.")
    return {"status": "present", "snapshot_name": name, "read_only": True,
            "integrity_verified": False, "manifest": json.loads(manifest.read_text(encoding="utf-8"))}


def inspect_market_series(database, request, *, as_of):
    from .storage import CrossAssetStore

    values = request_fields(request, {"series_id", "data_mode", "start", "end", "max_rows", "all_vintages"}, ("series_id", "data_mode"))
    values.setdefault("max_rows", 128)
    if type(values["max_rows"]) is not int or not 1 <= values["max_rows"] <= 512:
        raise ValueError("Native interval inspection max_rows must be between 1 and 512; narrow start/end for larger histories.")
    result = CrossAssetStore(database).read_series(as_of=as_of, **values)
    return result if result is not None else {"status": "missing", "read_only": True}


def market_fleet(settings, name, request, *, as_of):
    from .prices import timestamp
    from .snapshots import MarketSnapshot

    values = request_fields(request, {"series_ids", "data_mode", "start", "end", "max_rows"},
                     ("series_ids", "data_mode", "start", "end"))
    ids = values.pop("series_ids")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 1000
            or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids)):
        raise ValueError("Fleet requires 1..1000 distinct explicit series IDs.")
    mode = values.pop("data_mode")
    if mode not in {"live", "import", "demo"}:
        raise ValueError("Fleet data_mode must be live, import or demo.")
    values.setdefault("max_rows", 1_000_000)
    for key, value in (("as_of", as_of), ("start", values["start"]), ("end", values["end"])):
        timestamp(value, key)
    if values["start"] >= values["end"]:
        raise ValueError("Fleet start must precede end.")
    if type(values["max_rows"]) is not int or not 1 <= values["max_rows"] <= 1_000_000:
        raise ValueError("Fleet max_rows must be between 1 and 1000000.")
    directory = market_snapshot_path(settings, name)
    if not (directory / "manifest.json").is_file():
        return {"status": "missing", "snapshot_name": name, "read_only": True,
                "integrity_verified": False, "requested_series": len(ids), "data_mode": mode}
    snapshot = MarketSnapshot(directory)
    summary = snapshot.fleet_summary(ids, as_of=as_of, data_mode=mode, **values)
    return {**summary, "status": "ok", "as_of": as_of,
            "snapshot_name": name, "integrity_verified": True, "data_mode": mode,
            "requested_series": len(ids),
            "notice": "Native currencies, units and price bases stay separate. This is retained snapshot research, not live provider acceptance."}


