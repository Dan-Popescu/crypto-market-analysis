"""Explicit futures mark sensitivity and point-in-time unadjusted derivation.

Neither helper executes orders. A derived continuous series is a separate
identity and never supplies cash-plan fills or a tradable roll transaction.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any

from ..evidence import content_hash
from .prices import (
    MAX_BAR_VINTAGES,
    _bars_as_of_validated,
    currency,
    effective_known_at,
    number,
    object_fields,
    receipt_clocks,
    source_url,
    text_field,
    timestamp,
    validate_series,
)


def validate_futures_contract(contract: Any) -> dict:
    """Validate the registry contract; currency belongs to its native listing."""
    contract = object_fields(contract, {"expiry", "multiplier", "unit", "settlement"}, {"underlying_instrument_id"}, "futures contract")
    timestamp(contract["expiry"], "futures contract.expiry")
    number(contract["multiplier"], "futures contract.multiplier", positive=True)
    text_field(contract["unit"], "futures contract.unit")
    text_field(contract["settlement"], "futures contract.settlement")
    if "underlying_instrument_id" in contract:
        text_field(contract["underlying_instrument_id"], "futures contract.underlying_instrument_id")
    return dict(contract)


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} overflows the supported numerical range")
    return value


def value_futures_position(contract: Any, request: Any, *, as_of: int, native_currency: str) -> dict:
    """One mark's entry-to-mark P&L and collateral under declared fixed margins.

    Negative/zero prices are admissible: value is a difference times the signed
    contract count and multiplier. Margin and collateral are cash amounts, not
    invented percentage returns on a possibly nonpositive contract price. This
    isolated mark cannot prove that a position survived earlier margin calls.
    """
    contract = validate_futures_contract(contract)
    timestamp(as_of, "as_of")
    currency(native_currency, "native_currency")
    request = object_fields(
        request,
        {"currency", "quantity", "entry_price", "entry_at", "mark_price", "mark_at", "mark_observed_at",
         "collateral", "initial_margin_per_contract", "maintenance_margin_per_contract", "fees"},
        set(), "futures valuation",
    )
    if currency(request["currency"], "futures valuation.currency") != native_currency:
        raise ValueError("futures valuation currency must match its native listing; no implicit FX")
    quantity = request["quantity"]
    if type(quantity) is not int or quantity == 0:
        raise ValueError("futures valuation.quantity must be a nonzero signed integer contract count")
    number(quantity, "futures valuation.quantity")
    for clock in ("entry_at", "mark_at", "mark_observed_at"):
        timestamp(request[clock], f"futures valuation.{clock}")
    if not request["entry_at"] <= request["mark_at"] <= request["mark_observed_at"] <= as_of:
        raise ValueError("futures entry, mark, receipt and cutoff clocks must be ordered")
    if as_of >= contract["expiry"] or request["mark_at"] >= contract["expiry"]:
        raise ValueError("post-expiry settlement or delivery requires a separate model")
    entry = number(request["entry_price"], "futures valuation.entry_price")
    mark = number(request["mark_price"], "futures valuation.mark_price")
    collateral = number(request["collateral"], "futures valuation.collateral", minimum=0)
    initial = number(request["initial_margin_per_contract"], "futures valuation.initial_margin_per_contract", positive=True)
    maintenance = number(request["maintenance_margin_per_contract"], "futures valuation.maintenance_margin_per_contract", minimum=0)
    if maintenance > initial:
        raise ValueError("maintenance margin cannot exceed initial margin in this declared fixed-margin model")
    fees = None if request["fees"] is None else number(request["fees"], "futures valuation.fees", minimum=0)
    initial_required = _finite(abs(quantity) * initial, "required initial margin")
    maintenance_required = _finite(abs(quantity) * maintenance, "required maintenance margin")
    if collateral - (fees or 0) < initial_required:
        raise ValueError("collateral after declared fees is below the initial margin requirement")
    pnl = _finite((mark - entry) * quantity * contract["multiplier"], "marked futures P&L")
    equity = _finite(collateral + pnl - (fees or 0), "collateral equity")
    return {
        "schema": "futures-mark.v1", "as_of": as_of, "currency": native_currency, "unit": contract["unit"],
        "contract": contract, "quantity": quantity, "model": "single_mark_fixed_cash_margin_sensitivity",
        "entry_at": request["entry_at"], "mark_at": request["mark_at"], "mark_observed_at": request["mark_observed_at"],
        "entry_price": entry, "mark_price": mark, "entry_to_mark_pnl": pnl,
        "collateral": collateral, "collateral_equity": equity,
        "initial_margin_required": initial_required, "maintenance_margin_required": maintenance_required,
        "margin_status": "below_maintenance" if equity < maintenance_required else "at_or_above_maintenance",
        "cash_top_up_to_initial": max(0.0, initial_required - equity),
        "fees": {"status": "explicit" if fees is not None else "unknown_excluded", "amount": fees},
        "executable": False,
        "limitations": [
            "Hypothetical mark sensitivity, not a cash-funded spot holding or percentage price return.",
            "No daily settlement path, earlier margin-call survival, liquidation, funding interest or changing margin schedule is inferred.",
            "Expiry delivery and settlement are unsupported; no roll transaction is synthesized.",
        ],
    }


def build_continuous_series(document: Any, *, as_of: int) -> dict:
    """Select raw source bars using explicit decisions known before their roll.

    Only unadjusted splicing is implemented. Roll decisions name their source
    contract and the clock at which that contract was in the available chain.
    No liquidity rank, future expiry listing, back adjustment or fill is guessed.
    A roll at an interval boundary applies from the source bar's start.
    """
    timestamp(as_of, "as_of")
    document = object_fields(
        document, {"schema", "descriptor", "method", "version", "contracts", "rolls"}, set(), "continuous derivation",
    )
    if document["schema"] != "continuous-futures-request.v1":
        raise ValueError("continuous derivation.schema must be continuous-futures-request.v1")
    if document["method"] != "unadjusted":
        raise ValueError("only explicit unadjusted continuous splicing is supported")
    text_field(document["version"], "continuous derivation.version")
    target = validate_series({"schema": "market-series.v1", "descriptor": document["descriptor"], "bars": []})
    descriptor = target["descriptor"]
    if descriptor["price_basis"] != "continuous" or descriptor["listing_id"] is not None:
        raise ValueError("continuous derivation requires a derived continuous identity without a tradable listing")
    if not isinstance(document["contracts"], list) or not isinstance(document["rolls"], list):
        raise ValueError("continuous contracts and rolls must be arrays")
    if len(document["contracts"]) > 1_000 or len(document["rolls"]) > 2_000:
        raise ValueError("continuous derivation exceeds the bounded 1000 contract or 2000 roll limit")
    contracts: dict[str, dict] = {}
    source_bar_count = 0
    for entry in document["contracts"]:
        entry = object_fields(entry, {"instrument_id", "available_at", "contract", "series"}, set(), "continuous source contract")
        identifier = text_field(entry["instrument_id"], "continuous source instrument_id")
        timestamp(entry["available_at"], "continuous source available_at")
        specification = validate_futures_contract(entry["contract"])
        source = validate_series(entry["series"])
        source_bar_count += len(source["bars"])
        if source_bar_count > MAX_BAR_VINTAGES:
            raise ValueError("continuous derivation exceeds the bounded aggregate source vintage limit")
        source_descriptor = source["descriptor"]
        if identifier in contracts:
            raise ValueError("continuous source contract identities must be unique")
        if identifier == descriptor["instrument_id"] or source_descriptor["id"] == descriptor["id"]:
            raise ValueError("derived continuous identity must differ from every tradable source contract and series")
        if source_descriptor["instrument_id"] != identifier or source_descriptor["price_basis"] != "raw":
            raise ValueError("continuous source must identify a raw individual futures contract")
        if source_descriptor["listing_id"] is None or source["actions"]:
            raise ValueError("continuous source requires an individual contract listing without equity corporate actions")
        for field in ("native_currency", "unit", "interval", "calendar_id", "calendar_version", "data_mode"):
            if source_descriptor[field] != descriptor[field]:
                raise ValueError(f"continuous source {field} must match the declared derived series")
        if specification["unit"] != descriptor["unit"]:
            raise ValueError("continuous contract unit does not match its native series")
        contracts[identifier] = {**entry, "contract": specification, "series": source}
    rolls: list[dict] = []
    seen: set[int] = set()
    for roll in document["rolls"]:
        roll = object_fields(roll, {"effective_at", "known_at", "instrument_id", "reason", "source_url"}, set(), "continuous roll")
        for clock in ("effective_at", "known_at"):
            timestamp(roll[clock], f"continuous roll.{clock}")
        text_field(roll["instrument_id"], "continuous roll.instrument_id")
        text_field(roll["reason"], "continuous roll.reason")
        source_url(roll["source_url"], "continuous roll.source_url")
        if roll["known_at"] > as_of:
            # Later knowledge cannot alter the available historical chain.
            continue
        if roll["instrument_id"] not in contracts:
            raise ValueError("known continuous roll refers to an unknown source contract")
        contract = contracts[roll["instrument_id"]]
        if roll["known_at"] > roll["effective_at"] or contract["available_at"] > roll["known_at"]:
            raise ValueError("roll decisions must use chain information available before their effective instant")
        if roll["effective_at"] >= contract["contract"]["expiry"]:
            raise ValueError("continuous roll cannot select an expired contract")
        if roll["effective_at"] in seen:
            raise ValueError("continuous roll effective instants must be unique")
        seen.add(roll["effective_at"])
        if roll["effective_at"] <= as_of:
            rolls.append(dict(roll))
    rolls.sort(key=lambda roll: roll["effective_at"])
    roll_times = [roll["effective_at"] for roll in rolls]
    candidates: list[tuple[dict, str, dict]] = []
    for identifier, contract in contracts.items():
        clocks = receipt_clocks(contract["series"], "bars")
        for bar in _bars_as_of_validated(contract["series"], as_of):
            roll_index = bisect_right(roll_times, bar["start"]) - 1
            if roll_index < 0 or rolls[roll_index]["instrument_id"] != identifier:
                continue
            next_roll = rolls[roll_index + 1] if roll_index + 1 < len(rolls) else None
            if next_roll and next_roll["effective_at"] < bar["end"]:
                raise ValueError("a roll inside a native source interval requires a finer source interval; no synthetic split")
            if bar["end"] > contract["contract"]["expiry"]:
                raise ValueError("source observation extends beyond contract expiry")
            derived_bar = dict(bar)
            derived_bar["observed_at"] = max(
                effective_known_at(contract["series"], bar, "bars", receipt_index=clocks),
                rolls[roll_index]["known_at"], contract["available_at"],
            )
            candidates.append((derived_bar, identifier, bar))
    candidates.sort(key=lambda item: (item[0]["start"], item[0]["end"]))
    target["bars"] = [dict(bar) for bar, _, _ in candidates]
    if any(
        entry["series"]["descriptor"]["revision_semantics"] == "captured_history"
        or entry["series"].get("storage_provenance", {}).get("receipt_evidence") == "self_declared_history"
        for entry in contracts.values() if entry["available_at"] <= as_of
    ):
        target["descriptor"]["revision_semantics"] = "captured_history"
    # This validates native overlap without mutating the source series identities.
    target = validate_series(target)
    return {
        "schema": "continuous-futures.v1", "series": target, "as_of": as_of,
        "derivation": {
            "method": "unadjusted", "version": document["version"], "rolls": rolls,
            "source_bars": [{"instrument_id": identifier, "start": bar["start"], "end": bar["end"],
                             "observed_at": original["observed_at"], "effective_known_at": bar["observed_at"],
                             "hash": content_hash(original)} for bar, identifier, original in candidates],
            "contracts": [{"instrument_id": key, "available_at": entry["available_at"], "contract": entry["contract"]}
                          for key, entry in sorted(contracts.items()) if entry["available_at"] <= as_of],
        },
        "executable": False,
        "limitations": [
            "Unadjusted synthetic path includes roll gaps and is not executable contract P&L.",
            "Chain availability and roll-decision clocks are supplied evidence declarations, not independently verified market knowledge.",
            "No later liquidity, future chain membership, synthetic fill or backward price adjustment selects the chain.",
        ],
    }
