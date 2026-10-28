"""Canonical identities and captured analytical evidence shared by every interface."""

from __future__ import annotations

import hashlib
import json
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


def metric_dimension(observation: Observation) -> dict:
    return {"quote": observation.quote, "unit": observation.unit,
            **{key: observation.metadata[key] for key in (
                "expiry", "instrument", "symbol", "funding_interval", "funding_interval_hours",
                "aggregation", "original_dimension",
            ) if key in observation.metadata}}


def metric_identity(observation: Observation) -> tuple:
    return (observation.source, observation.asset, observation.metric,
            observation.data_mode, canonical_json(metric_dimension(observation)))


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

    def evidence(self) -> dict:
        """Return an independent JSON envelope, suitable for immutable local issuance."""
        return json.loads(canonical_json(asdict(self)))

    @property
    def reference(self) -> dict:
        return {"schema_version": "2.0", "as_of": self.as_of, "asset": self.asset,
                "source": self.source, "quote": self.quote, "data_mode": self.data_mode,
                "rule_version": "captured-inputs-1", "configuration_hash": content_hash(self.configuration),
                "content_hash": content_hash(self.evidence()), "watermark": dict(self.watermark)}
