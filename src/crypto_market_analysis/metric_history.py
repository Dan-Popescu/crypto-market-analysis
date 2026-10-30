"""Validated selections and opaque continuation tokens for stored metric history."""

import base64
import binascii
import hashlib
import json
import time

from .asset_universe import validate_asset


def selection(asset, metric, mode, source, quote, unit, dimension, as_of, start, end):
    if asset not in ("MACRO", "STABLECOINS"):
        validate_asset(asset)
    if mode not in ("live", "demo", "import"):
        raise ValueError("Unsupported history data mode.")
    for name, value in (("metric", metric), ("source", source), ("quote", quote), ("unit", unit)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 256):
            raise ValueError(f"{name} must be a nonempty string of at most 256 characters.")
    if dimension is not None:
        if not isinstance(dimension, dict) or len(dimension) > 32:
            raise ValueError("dimension must be a complete metric dimension object.")
        try:
            encoded = json.dumps(dimension, sort_keys=True, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("dimension must contain finite JSON values.") from exc
        if len(encoded) > 8192:
            raise ValueError("dimension is too large.")
        if "quote" not in dimension or "unit" not in dimension:
            raise ValueError("dimension must include quote and unit; use metric_series for discovery.")
        if ((quote is not None and dimension["quote"] != quote)
                or (unit is not None and dimension["unit"] != unit)):
            raise ValueError("dimension conflicts with quote or unit.")
    for name, value in (("as_of", as_of), ("start", start), ("end", end)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a nonnegative integer UTC epoch second.")
    if as_of is not None and as_of > int(time.time()):
        raise ValueError("as_of cannot be in the future.")
    if start is not None and end is not None and start > end:
        raise ValueError("start must not exceed end.")
    if as_of is not None and end is not None and end > as_of:
        raise ValueError("end must not exceed as_of.")
    return dict(asset=asset, metric=metric, mode=mode, source=source, quote=quote,
                unit=unit, dimension=dimension, as_of=as_of, start=start, end=end)


def encode_cursor(value):
    raw = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()).decode()


def decode_cursor(value):
    try:
        if not isinstance(value, str) or len(value) > 32768:
            raise ValueError
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        body, checksum = raw[:-32], raw[-32:]
        if hashlib.sha256(body).digest() != checksum:
            raise ValueError
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid metric history cursor.") from exc
