"""Event-time freshness, independent of collection success or ingestion age."""

from __future__ import annotations

from .config import Settings
from .models import Observation


def assess_freshness(event_time: int | None, observed_at: int | None, as_of: int,
                     cadence_seconds: int, *, max_age_seconds: int | None = None,
                     next_release_time: int | None = None, schedule_verified: bool = True) -> dict:
    for name, value in (("as_of", as_of), ("cadence_seconds", cadence_seconds)):
        if isinstance(value, bool) or not isinstance(value, int) or (name == "cadence_seconds" and value <= 0):
            raise ValueError(f"{name} must be an integer with a positive cadence.")
    lifetime = cadence_seconds * 2 if max_age_seconds is None else max_age_seconds
    for name, value in (("event_time", event_time), ("observed_at", observed_at), ("next_release_time", next_release_time)):
        if value is not None and type(value) is not int:
            raise ValueError(f"{name} must be integer UTC epoch seconds or unknown.")
    if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
        raise ValueError("max_age_seconds must be a positive integer.")
    known = event_time is not None and observed_at is not None
    available = known and event_time <= as_of and observed_at <= as_of
    age = as_of - event_time if known else None
    expires = (next_release_time + cadence_seconds if next_release_time is not None
               else event_time + lifetime if event_time is not None else None)
    stale = bool(available and expires is not None and as_of >= expires)
    status = ("unavailable" if known and not available else "unknown" if not known
              else "stale" if stale else "fresh" if schedule_verified else "unknown")
    return {"status": status, "event_time": event_time, "observed_at": observed_at,
            "age_seconds": age, "ingestion_age_seconds": as_of - observed_at if observed_at is not None else None,
            "cadence_seconds": cadence_seconds, "max_age_seconds": lifetime, "expires_at": expires,
            "schedule_verified": schedule_verified, "stale": stale,
            "usable": status == "fresh"}


def metric_freshness(observation: Observation, settings: Settings, as_of: int) -> dict:
    if observation.metric == "spread_bps" or observation.metric.startswith("depth_"):
        family, cadence = "liquidity", settings.liquidity_interval
    elif observation.metric.startswith("option_") or observation.source == "deribit":
        family, cadence = "options", settings.options_interval
    elif observation.metric.startswith(("funding_", "open_interest")):
        family, cadence = "derivatives", settings.market_interval
    elif observation.asset == "MACRO":
        family, cadence = "macro", settings.context_interval
    elif observation.source == "coinmetrics":
        family, cadence = "network", settings.context_interval
    else:
        family, cadence = "context", settings.context_interval
    release = observation.metadata.get("next_release_time")
    if isinstance(release, bool) or not isinstance(release, int):
        release = None
    result = assess_freshness(observation.event_time, observation.observed_at, as_of, cadence,
                              next_release_time=release,
                              schedule_verified=family != "macro" or release is not None)
    return {**result, "family": family, "timestamp_source": observation.metadata.get("timestamp_source", "event")}
