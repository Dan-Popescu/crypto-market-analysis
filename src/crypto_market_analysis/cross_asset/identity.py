"""Bounded, bitemporal instrument identities, independent of provider tickers.

Intervals are half open and clocks are integer UTC epoch seconds. Registries are
append-only by convention: a revision keeps its logical key and adds a later
``known_at``. Corrections never alter what an earlier knowledge cutoff sees.
Instrument metadata is static; listings, aliases and universe membership carry
the effective/knowledge clocks. These identities confer no trading capability.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable
from typing import Any

SCHEMA = "market-registry.v1"
MAX_INSTRUMENTS = 1_000
MAX_ROWS = 50_000
MAX_BYTES = 16 * 1024 * 1024
ASSET_CLASSES = frozenset({"crypto", "equity", "commodity", "fx", "index", "other"})
FORMS = frozenset({"spot", "share", "fund", "reference", "future"})


class IdentityError(ValueError):
    """An invalid registry or unresolvable point-in-time identity."""


class UnknownAliasError(IdentityError):
    """No known and effective alias/listing satisfies the query."""


class AmbiguousAliasError(IdentityError):
    """More than one identity satisfies the query; disambiguation is required."""


def _fields(row: Any, required: set[str], optional: set[str], label: str) -> None:
    if not isinstance(row, dict) or not all(isinstance(key, str) for key in row):
        raise IdentityError(f"{label} must be an object with string keys")
    if required - row.keys() or row.keys() - required - optional:
        raise IdentityError(f"{label} has missing or unknown fields")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or value != value.strip():
        raise IdentityError(f"{label} must be a nonempty trimmed string of at most 256 characters")
    if any(ord(character) < 32 for character in value):
        raise IdentityError(f"{label} must not contain control characters")
    return value


def _clock(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 253402300799:
        raise IdentityError(f"{label} must be integer UTC epoch seconds")
    return value


def _interval(row: dict, start: str, end: str) -> None:
    _clock(row[start], start)
    if row[end] is not None and _clock(row[end], end) <= row[start]:
        raise IdentityError(f"{end} must be later than {start}")
    _clock(row["known_at"], "known_at")


def _rows(document: dict, key: str, limit: int) -> list:
    rows = document.get(key, [])
    if not isinstance(rows, list) or len(rows) > limit:
        raise IdentityError(f"{key} must be a list of at most {limit} records")
    return rows


def _contract(instrument: dict, instrument_ids: set[str]) -> None:
    contract = instrument.get("contract")
    if instrument["form"] != "future":
        if contract is not None:
            raise IdentityError("contract details apply only to futures")
        return
    _fields(contract, {"expiry", "unit", "multiplier", "settlement"}, {"underlying_instrument_id"}, "contract")
    _clock(contract["expiry"], "expiry")
    _text(contract["unit"], "unit")
    _text(contract["settlement"], "settlement")
    multiplier = contract["multiplier"]
    if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or multiplier <= 0:
        raise IdentityError("contract multiplier must be positive and finite")
    if "underlying_instrument_id" in contract:
        _text(contract["underlying_instrument_id"], "underlying_instrument_id")
        if contract["underlying_instrument_id"] not in instrument_ids:
            raise IdentityError("contract underlying instrument does not exist")
        if contract["underlying_instrument_id"] == instrument["id"]:
            raise IdentityError("contract cannot be its own underlying")


def _unique_revisions(rows: list[dict], fields: tuple[str, ...], label: str) -> None:
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in fields) + (row["known_at"],)
        if key in seen:
            raise IdentityError(f"duplicate {label} revision")
        seen.add(key)


def validate_registry(document: dict) -> dict:
    """Return a defensive validated JSON copy, rejecting unknown or oversized data.

    A listing ID can have later-known lifecycle corrections. Its instrument,
    venue and native currency cannot change; those require a distinct listing.
    Alias collisions are representable and fail explicitly when resolved.
    """
    _fields(document, {"schema", "instruments", "listings", "aliases"}, {"memberships"}, "registry")
    if document["schema"] != SCHEMA:
        raise IdentityError(f"unsupported registry schema: expected {SCHEMA}")
    instruments = _rows(document, "instruments", MAX_INSTRUMENTS)
    listings = _rows(document, "listings", MAX_ROWS)
    aliases = _rows(document, "aliases", MAX_ROWS)
    memberships = _rows(document, "memberships", MAX_ROWS)
    try:
        encoded = json.dumps(document, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise IdentityError("registry must contain finite JSON data") from exc
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise IdentityError(f"registry exceeds {MAX_BYTES} bytes")
    instrument_ids: set[str] = set()
    for instrument in instruments:
        _fields(instrument, {"id", "name", "asset_class", "form"}, {"identifiers", "contract"}, "instrument")
        for key in ("id", "name", "asset_class", "form"):
            _text(instrument[key], key)
        if instrument["id"] in instrument_ids:
            raise IdentityError("duplicate instrument ID")
        instrument_ids.add(instrument["id"])
        if instrument["asset_class"] not in ASSET_CLASSES or instrument["form"] not in FORMS:
            raise IdentityError("unsupported asset class or instrument form")
        identifiers = instrument.get("identifiers", {})
        if not isinstance(identifiers, dict) or len(identifiers) > 32:
            raise IdentityError("identifiers must be a map of at most 32 published identifiers")
        for key, value in identifiers.items():
            _text(key, "identifier type")
            _text(value, "identifier")
    for instrument in instruments:
        _contract(instrument, instrument_ids)
    instrument_by_id = {row["id"]: row for row in instruments}
    listing_identities: dict[str, tuple] = {}
    for listing in listings:
        _fields(listing, {"id", "instrument_id", "native_currency", "calendar_id", "active_from", "active_until", "known_at"}, {"venue_mic", "venue_id"}, "listing")
        for key in ("id", "instrument_id", "native_currency", "calendar_id"):
            _text(listing[key], key)
        venues = [key for key in ("venue_mic", "venue_id") if key in listing]
        if len(venues) != 1:
            raise IdentityError("listing requires exactly one venue_mic or venue_id")
        _text(listing[venues[0]], venues[0])
        if listing["instrument_id"] not in instrument_ids:
            raise IdentityError("listing instrument does not exist")
        if instrument_by_id[listing["instrument_id"]]["form"] == "reference":
            raise IdentityError("nontradable references cannot have listings")
        _interval(listing, "active_from", "active_until")
        identity = (listing["instrument_id"], listing["native_currency"], venues[0], listing[venues[0]])
        if listing["id"] in listing_identities and listing_identities[listing["id"]] != identity:
            raise IdentityError("listing identity cannot change across revisions")
        listing_identities[listing["id"]] = identity
    for alias in aliases:
        _fields(alias, {"provider", "symbol", "instrument_id", "listing_id", "effective_from", "effective_until", "known_at"}, set(), "alias")
        for key in ("provider", "symbol", "instrument_id"):
            _text(alias[key], key)
        if alias["instrument_id"] not in instrument_ids:
            raise IdentityError("alias instrument does not exist")
        if alias["listing_id"] is not None:
            _text(alias["listing_id"], "listing_id")
            listing = listing_identities.get(alias["listing_id"])
            if listing is None or listing[0] != alias["instrument_id"]:
                raise IdentityError("alias listing does not match its instrument")
        _interval(alias, "effective_from", "effective_until")
    for membership in memberships:
        _fields(membership, {"universe", "instrument_id", "valid_from", "valid_until", "known_at"}, set(), "membership")
        _text(membership["universe"], "universe")
        _text(membership["instrument_id"], "instrument_id")
        if membership["instrument_id"] not in instrument_ids:
            raise IdentityError("membership instrument does not exist")
        _interval(membership, "valid_from", "valid_until")
    _unique_revisions(listings, ("id",), "listing")
    _unique_revisions(aliases, ("provider", "symbol", "instrument_id", "listing_id", "effective_from"), "alias")
    _unique_revisions(memberships, ("universe", "instrument_id", "valid_from"), "membership")
    result = copy.deepcopy(document)
    result.setdefault("memberships", [])
    return result


def _latest(rows: Iterable[dict], fields: tuple[str, ...], as_of: int) -> list[dict]:
    versions: dict[tuple, dict] = {}
    for row in rows:
        if row["known_at"] <= as_of:
            key = tuple(row[field] for field in fields)
            if key not in versions or row["known_at"] > versions[key]["known_at"]:
                versions[key] = row
    return list(versions.values())


def _effective(row: dict, at: int, start: str, end: str) -> bool:
    return row[start] <= at and (row[end] is None or at < row[end])


def registry_view(registry: dict, *, as_of: int, effective_at: int, universe: str | None = None) -> dict:
    """Project known, effective identities without hiding historical delistings.

    Query a historical effective time to see a delisted listing or former universe
    member. A later knowledge cutoff may expose a correction to that history.
    Static instruments appear only through a known effective listing, alias or
    membership, avoiding future introductions in a historical universe.
    """
    document = validate_registry(registry)
    _clock(as_of, "as_of")
    _clock(effective_at, "effective_at")
    if universe is not None:
        _text(universe, "universe")
    memberships = [row for row in _latest(document["memberships"], ("universe", "instrument_id", "valid_from"), as_of)
                   if _effective(row, effective_at, "valid_from", "valid_until")]
    listings = [row for row in _latest(document["listings"], ("id",), as_of)
                if _effective(row, effective_at, "active_from", "active_until")]
    listing_ids = {row["id"] for row in listings}
    aliases = [row for row in _latest(document["aliases"], ("provider", "symbol", "instrument_id", "listing_id", "effective_from"), as_of)
               if _effective(row, effective_at, "effective_from", "effective_until")
               and (row["listing_id"] is None or row["listing_id"] in listing_ids)]
    visible_ids = {row["instrument_id"] for row in listings + aliases + memberships}
    if universe is not None:
        visible_ids &= {row["instrument_id"] for row in memberships if row["universe"] == universe}
        memberships = [row for row in memberships if row["universe"] == universe]
    return {
        "schema": "market-registry-view.v1", "as_of": as_of, "effective_at": effective_at, "universe": universe,
        "instruments": [row for row in document["instruments"] if row["id"] in visible_ids],
        "listings": [row for row in listings if row["instrument_id"] in visible_ids],
        "aliases": [row for row in aliases if row["instrument_id"] in visible_ids],
        "memberships": [row for row in memberships if row["instrument_id"] in visible_ids],
    }


def resolve_alias(registry: dict, *, provider: str, symbol: str, effective_at: int,
                  as_of: int, listing_id: str | None = None) -> dict:
    """Resolve one provider alias, with explicit ambiguity and no latest-data leak."""
    _text(provider, "provider")
    _text(symbol, "symbol")
    if listing_id is not None:
        _text(listing_id, "listing_id")
    view = registry_view(registry, as_of=as_of, effective_at=effective_at)
    candidates = [row for row in view["aliases"] if row["provider"] == provider and row["symbol"] == symbol
                  and (listing_id is None or row["listing_id"] == listing_id)]
    identities = {(row["instrument_id"], row["listing_id"]) for row in candidates}
    if not identities:
        raise UnknownAliasError(f"no known effective alias for {provider}:{symbol}")
    if len(identities) > 1:
        raise AmbiguousAliasError(f"ambiguous alias {provider}:{symbol}; specify a listing or provider symbol")
    alias = max(candidates, key=lambda row: (row["known_at"], row["effective_from"]))
    return {
        "instrument": next(row for row in view["instruments"] if row["id"] == alias["instrument_id"]),
        "listing": next((row for row in view["listings"] if row["id"] == alias["listing_id"]), None),
        "alias": alias,
    }


def legacy_crypto_identity(asset: str, quote: str, source: str) -> dict:
    """Project a legacy source/asset/quote key without reinterpreting its evidence.

    The legacy profile lacks network and venue identity. Namespace it separately;
    a source is a publisher, so this projection cannot invent a venue listing.
    Preserve the exact selectors, including case, and never mutate original rows.
    """
    for value, label in ((asset, "asset"), (quote, "quote"), (source, "source")):
        _text(value, label)
    legacy = {"asset": asset, "quote": quote, "source": source}
    digest = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    asset_digest = hashlib.sha256(asset.encode()).hexdigest()
    return {
        "schema": "legacy-crypto-identity.v1", "instrument_id": f"legacy:crypto:{asset_digest}",
        "listing_id": None, "provider": source, "native_currency": quote,
        "asset_class": "crypto", "form": "spot", "legacy": legacy, "legacy_key_sha256": digest,
    }
