"""Canonical identities and captured analytical evidence shared by every interface."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import zlib
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from pathlib import Path

from .models import Candle, Observation


def canonical_json(value: object) -> str:
    def encode(item):
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if isinstance(item, (Path, Decimal)):
            return str(item)
        raise TypeError(f"Unsupported evidence value: {type(item).__name__}")

    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), default=encode)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def funding_period_seconds(metadata: dict) -> int | float | None:
    """Normalize published unit-bearing aliases; conflicting or unitless periods stay unknown."""
    values = []
    for key, multiplier in (("funding_interval_seconds", 1), ("interval_seconds", 1),
                            ("funding_interval_hours", 3600), ("funding_period_hours", 3600)):
        value = metadata.get(key)
        if type(value) in (float, int) and math.isfinite(value) and value > 0:
            values.append(value * multiplier)
    raw = metadata.get("funding_interval")
    if isinstance(raw, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", raw, re.I)
        if match:
            values.append(float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2].lower()])
    if not values or any(not math.isfinite(value) or value <= 0 or value != values[0] for value in values):
        return None
    return int(values[0]) if float(values[0]).is_integer() else values[0]


def metric_dimension(observation: Observation) -> dict:
    result = {"quote": observation.quote, "unit": observation.unit,
            **{key: observation.metadata[key] for key in (
                "expiry", "instrument", "symbol",
                "aggregation", "original_dimension",
                "contract_size", "market_type", "settle",
                "band_percent", "counting_convention", "value_kind",
            ) if key in observation.metadata}}
    period_fields = {key: observation.metadata[key] for key in (
        "funding_interval", "funding_interval_hours", "funding_period_hours", "funding_interval_seconds", "interval_seconds"
    ) if key in observation.metadata}
    if period_fields:
        period = funding_period_seconds(observation.metadata)
        result["funding_interval_seconds" if period is not None else "unknown_funding_period"] = (
            period if period is not None else period_fields)
    return result


def metric_identity(observation: Observation) -> tuple:
    return (observation.source, observation.asset, observation.metric,
            observation.data_mode, canonical_json(metric_dimension(observation)))


MAX_EVIDENCE_BYTES = 32 * 1024 * 1024


def estimated_evidence_bytes(evidence: dict) -> int:
    """Return the canonical uncompressed size used by the issuance budget."""
    return len(canonical_json(evidence).encode("utf-8"))


def archive_evidence(evidence: dict) -> dict:
    """Store a bounded lossless envelope instead of duplicating large JSON trees."""
    raw = canonical_json(evidence).encode("utf-8")
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("Consumed evidence exceeds the 32 MiB uncompressed issuance limit.")
    return {"schema_version": 1, "encoding": "zlib+base64", "sha256": hashlib.sha256(raw).hexdigest(),
            "uncompressed_bytes": len(raw), "candles_count": len(evidence.get("candles", [])),
            "observations_count": len(evidence.get("observations", [])),
            "data": base64.b64encode(zlib.compress(raw)).decode("ascii")}


def restore_evidence(evidence: dict) -> dict:
    """Read lossless evidence, retaining compatibility with early raw envelopes."""
    if evidence.get("encoding") != "zlib+base64":
        return json.loads(canonical_json(evidence))
    try:
        size = evidence["uncompressed_bytes"]
        if type(size) is not int or not 0 <= size <= MAX_EVIDENCE_BYTES:
            raise ValueError("Invalid evidence size.")
        compressed = base64.b64decode(evidence["data"], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_EVIDENCE_BYTES + 1)
        if (len(raw) != size or len(raw) > MAX_EVIDENCE_BYTES or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail
                or hashlib.sha256(raw).hexdigest() != evidence["sha256"]):
            raise ValueError("Evidence archive checksum or size mismatch.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Evidence must contain a JSON object.")
        return result
    except (KeyError, TypeError, zlib.error, UnicodeError) as exc:
        raise ValueError("Invalid evidence archive.") from exc


@dataclass(frozen=True)
class CapturedInputs:
    as_of: int
    asset: str
    data_mode: str
    source: str | None
    quote: str | None
    candles: tuple[Candle, ...]
    observations: tuple[Observation, ...]
    coverage: tuple[dict, ...]
    events: tuple[dict, ...]
    source_health: dict
    watermark: dict
    configuration: dict
    truncation: dict
    calendar: tuple[dict, ...] = ()
    capture_profile: dict | None = None

    def evidence(self) -> dict:
        """Return an independent JSON envelope, suitable for immutable local issuance."""
        result = asdict(self)
        if self.capture_profile is None:
            result.pop("capture_profile")
        return json.loads(canonical_json(result))

    @property
    def estimated_evidence_bytes(self) -> int:
        return estimated_evidence_bytes(self.evidence())

    @property
    def reference(self) -> dict:
        availability = [row.observed_at for row in (*self.candles, *self.observations)]
        availability.extend(row["observed_at"] for row in (*self.events, *self.calendar)
                            if type(row.get("observed_at")) is int)
        result = {"schema_version": "2.0", "as_of": self.as_of, "asset": self.asset,
                  "source": self.source, "quote": self.quote, "data_mode": self.data_mode,
                  "available_at": max(availability, default=0),
                  "rule_version": "captured-inputs-1", "configuration_hash": content_hash(self.configuration),
                  "content_hash": content_hash(self.evidence()), "watermark": dict(self.watermark)}
        if self.capture_profile and self.capture_profile.get("dataset_lineage"):
            result["dataset_lineage"] = json.loads(canonical_json(self.capture_profile["dataset_lineage"]))
        return result
