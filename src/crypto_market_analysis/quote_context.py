"""Pure, bounded native-quote context; reference FX never becomes an execution rate."""

from __future__ import annotations

import copy
import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from urllib.parse import urlsplit

_MAX_OBSERVATIONS = 100
_MAX_BYTES = 1024 * 1024
_MODES = {"live", "import", "demo"}


def _epoch(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative epoch second.")
    return value


def _positive_decimal(value: Any) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Quote values must be positive finite decimal strings or integers.")
    if len(str(value)) > 100:
        raise ValueError("Quote value exceeds the decimal size limit.")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Quote values must be positive finite decimals.") from exc
    if not result.is_finite() or result <= 0 or not -30 <= result.adjusted() <= 30:
        raise ValueError("Quote values must be positive finite decimals with exponent between -30 and 30.")
    if len(result.as_tuple().digits) > 40:
        raise ValueError("Quote value exceeds the 40-digit precision limit.")
    return result


def _number(value: Decimal) -> str:
    return format(value, "f")


def _observation(row: Any, mode: str, as_of: int, max_age: int) -> tuple[dict, Decimal]:
    required = {
        "base", "quote", "value", "event_time", "observed_at", "source", "source_url",
        "data_version", "price_type", "data_mode",
    }
    if not isinstance(row, dict) or not required <= row.keys():
        raise ValueError("Quote observations require native pair, decimal value, clocks, source/version and type/mode.")
    if row["data_mode"] != mode:
        raise ValueError("All quote observations must use the requested data_mode.")
    if any(not isinstance(row[key], str) or not row[key].strip()
           for key in ("base", "quote", "source", "source_url", "data_version", "price_type")):
        raise ValueError("Quote pair, source/version and price type must be nonempty strings.")
    url = urlsplit(row["source_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Quote source_url must be an HTTPS provenance URL without credentials.")
    event_time = _epoch(row["event_time"], "event_time")
    observed_at = _epoch(row["observed_at"], "observed_at")
    if observed_at < event_time:
        raise ValueError("Quote observed_at cannot precede its event_time.")
    value = _positive_decimal(row["value"])
    if observed_at > as_of or event_time > as_of:
        status = "not_available_as_of"
    elif as_of - event_time > max_age:
        status = "stale"
    else:
        status = "available"
    return {
        "status": status,
        "observation": copy.deepcopy(row),
        "native_base": row["base"],
        "native_quote": row["quote"],
        "event_age_seconds": as_of - event_time if event_time <= as_of else None,
        "observation_age_seconds": as_of - observed_at if observed_at <= as_of else None,
        "observation_delay_seconds": observed_at - event_time,
        "available_at": observed_at,
    }, value


def analyze_quote_context(request: dict, as_of: int) -> dict[str, Any]:
    """Explain a dated FX reference and observed USD stablecoin/fiat basis.

    Request: data_mode (live/import/demo), optional fx_reference, stablecoin_prices
    (up to 100 observations), max_age_seconds (default 86400), and
    max_endpoint_skew_seconds (default 300). Each observation requires base, quote,
    value (decimal string), event_time, observed_at, source, HTTPS source_url,
    data_version, data_mode and price_type. FX must be EUR/USD or USD/EUR with
    price_type indicative_reference. Stablecoins must be USDT or USDC priced in
    USD or EUR with price_type observed_spot. Input assertions are preserved;
    this function does not qualify feeds, discover FX, or modify core series.

    USD observations compare directly with one USD per token. EUR observations
    require an available reference at a sufficiently close event endpoint and
    explicitly multiply native EUR/token by USD/EUR. Reciprocal FX orientation
    and every applied factor remain visible. No observation is averaged.
    """
    _epoch(as_of, "as_of")
    allowed = {"data_mode", "fx_reference", "stablecoin_prices", "max_age_seconds", "max_endpoint_skew_seconds"}
    if not isinstance(request, dict) or request.keys() - allowed:
        raise ValueError("Unsupported quote context request fields.")
    try:
        encoded = json.dumps(request, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Quote context must contain finite JSON values.") from exc
    if len(encoded.encode()) > _MAX_BYTES:
        raise ValueError("Quote context exceeds the one-megabyte limit.")
    mode = request.get("data_mode")
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("Quote context requires data_mode live, import or demo.")
    max_age = request.get("max_age_seconds", 86400)
    max_skew = request.get("max_endpoint_skew_seconds", 300)
    for name, value in (("max_age_seconds", max_age), ("max_endpoint_skew_seconds", max_skew)):
        if type(value) is not int or not 0 <= value <= 31_536_000:
            raise ValueError(f"{name} must be from zero to 31536000 seconds.")
    rows = request.get("stablecoin_prices", [])
    if not isinstance(rows, list) or len(rows) > _MAX_OBSERVATIONS:
        raise ValueError("Use at most 100 stablecoin price observations.")
    fx: dict[str, Any] = {"status": "missing", "eurusd_reference": None, "transformation": None}
    fx_value = None
    with localcontext() as context:
        context.prec = 50
        if request.get("fx_reference") is not None:
            row = request["fx_reference"]
            fx, raw_fx = _observation(row, mode, as_of, max_age)
            if (row["base"], row["quote"]) not in {("EUR", "USD"), ("USD", "EUR")}:
                raise ValueError("FX reference must explicitly identify EUR/USD or USD/EUR orientation.")
            if row["price_type"] != "indicative_reference":
                raise ValueError("FX context requires an indicative_reference, not an asserted executable rate.")
            inverse = row["base"] == "USD"
            fx.update(
                eurusd_reference=None,
                reference_unit="USD per EUR",
                executable=False,
                transformation={
                    "operation": "reciprocal" if inverse else "identity",
                    "formula": "1 / native_value" if inverse else "native_value",
                    "input_pair": f"{row['base']}/{row['quote']}",
                    "output_pair": "EUR/USD",
                    "input_data_version": row["data_version"],
                },
            )
            if fx["status"] == "available":
                fx_value = Decimal(1) / raw_fx if inverse else raw_fx
                fx["eurusd_reference"] = _number(fx_value)
        basis = []
        for row in rows:
            item, value = _observation(row, mode, as_of, max_age)
            if row["base"] not in {"USDT", "USDC"} or row["quote"] not in {"USD", "EUR"}:
                raise ValueError("Stablecoin basis requires directly observed USDT/USDC priced in USD or EUR.")
            if row["price_type"] != "observed_spot":
                raise ValueError("Stablecoin basis requires an observed_spot price.")
            item.update(
                basis_percent=None,
                usd_per_token=None,
                endpoint_skew_seconds=None,
                transformation=None,
                reference_parity="1 USD per token (comparison convention, not a redemption guarantee)",
            )
            if row["quote"] == "EUR" and "observation" in fx:
                item["endpoint_skew_seconds"] = abs(row["event_time"] - fx["observation"]["event_time"])
            if item["status"] != "available":
                basis.append(item)
                continue
            if row["quote"] == "USD":
                converted = value
                item["transformation"] = {"operation": "identity", "formula": "native USD/token", "input_data_version": row["data_version"]}
            elif fx_value is None:
                item["status"] = "fx_reference_" + fx["status"]
                basis.append(item)
                continue
            elif item["endpoint_skew_seconds"] > max_skew:
                item["status"] = "endpoint_skew_exceeded"
                basis.append(item)
                continue
            else:
                converted = value * fx_value
                item["transformation"] = {
                    "operation": "explicit_fx_conversion",
                    "formula": "native EUR/token * reference USD/EUR",
                    "native_value": _number(value),
                    "eurusd_reference": _number(fx_value),
                    "input_data_version": row["data_version"],
                    "fx_data_version": fx["observation"]["data_version"],
                    "fx_orientation_transform": fx["transformation"]["operation"],
                    "converted_unit": "USD per token",
                    "executable": False,
                }
            item.update(usd_per_token=_number(converted), basis_percent=_number((converted - 1) * 100))
            basis.append(item)
    return {
        "as_of": as_of,
        "data_mode": mode,
        "fx_reference": fx,
        "stablecoin_basis": basis,
        "limits": {"max_age_seconds": max_age, "max_endpoint_skew_seconds": max_skew, "max_observations": _MAX_OBSERVATIONS},
        "interpretation": "Descriptive local evidence only. FX references and converted basis are indicative, not executable rates. Native quotes remain separate; no cross-currency average or core-series conversion is applied.",
    }
