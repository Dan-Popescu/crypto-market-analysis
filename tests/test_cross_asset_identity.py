"""Synthetic identity acceptance cases; provider symbols are never global IDs."""

import copy
import hashlib
import json

import pytest

from crypto_market_analysis.cross_asset.identity import (
    AmbiguousAliasError,
    IdentityError,
    UnknownAliasError,
    legacy_crypto_identity,
    registry_view,
    resolve_alias,
    validate_registry,
)


def instrument(identifier="equity:abc", asset_class="equity", form="share"):
    return {"id": identifier, "name": identifier, "asset_class": asset_class, "form": form}


def listing(identifier="listing:usd", currency="USD", **changes):
    return {"id": identifier, "instrument_id": "equity:abc", "venue_mic": "XNYS",
            "native_currency": currency, "calendar_id": "xnys:test.v1", "active_from": 10,
            "active_until": None, "known_at": 10, **changes}


def alias(symbol="ABC", identifier="equity:abc", listing_id="listing:usd", **changes):
    return {"provider": "test", "symbol": symbol, "instrument_id": identifier,
            "listing_id": listing_id, "effective_from": 10, "effective_until": None,
            "known_at": 10, **changes}


def registry():
    return {"schema": "market-registry.v1", "instruments": [instrument()],
            "listings": [listing()], "aliases": [alias()]}


def resolve(document, at=20, cutoff=20, **changes):
    return resolve_alias(document, provider="test", symbol="ABC", effective_at=at, as_of=cutoff, **changes)


def test_ticker_collision_and_multiple_listings_require_explicit_identity():
    document = registry()
    document["instruments"].append(instrument("crypto:abc", "crypto", "spot"))
    document["aliases"].append(alias(identifier="crypto:abc", listing_id=None))
    with pytest.raises(AmbiguousAliasError):
        resolve(document)
    assert resolve(document, listing_id="listing:usd")["instrument"]["id"] == "equity:abc"
    document["aliases"].pop()
    document["listings"].append(listing("listing:eur", "EUR", venue_mic="XPAR"))
    document["aliases"].append(alias(listing_id="listing:eur"))
    with pytest.raises(AmbiguousAliasError):
        resolve(document)
    assert resolve(document, listing_id="listing:eur")["listing"]["native_currency"] == "EUR"


def test_late_delisting_and_membership_corrections_preserve_historical_cutoffs():
    document = registry()
    document["listings"].append(listing(active_until=30, known_at=50))
    document["memberships"] = [
        {"universe": "sample", "instrument_id": "equity:abc", "valid_from": 10,
         "valid_until": None, "known_at": 10},
        {"universe": "sample", "instrument_id": "equity:abc", "valid_from": 10,
         "valid_until": 30, "known_at": 50},
    ]
    assert resolve(document, at=40, cutoff=40)["listing"]["id"] == "listing:usd"
    with pytest.raises(UnknownAliasError):
        resolve(document, at=40, cutoff=50)
    assert resolve(document, at=29, cutoff=50)["listing"]["id"] == "listing:usd"
    before = registry_view(document, as_of=40, effective_at=40, universe="sample")
    assert len(before["instruments"]) == 1
    assert registry_view(document, as_of=50, effective_at=40, universe="sample")["instruments"] == []
    assert len(registry_view(document, as_of=50, effective_at=29, universe="sample")["instruments"]) == 1


def test_alias_reuse_and_later_known_revision_do_not_erase_disjoint_windows():
    document = registry()
    document["instruments"].append(instrument("equity:second"))
    document["aliases"] = [alias(effective_until=30),
                           alias(identifier="equity:second", listing_id=None, effective_from=30, known_at=30)]
    assert resolve(document, at=29, cutoff=50)["instrument"]["id"] == "equity:abc"
    assert resolve(document, at=30, cutoff=50)["instrument"]["id"] == "equity:second"
    document["aliases"].append(alias(effective_until=25, known_at=50))
    assert resolve(document, at=28, cutoff=49)["instrument"]["id"] == "equity:abc"
    with pytest.raises(UnknownAliasError):
        resolve(document, at=28, cutoff=50)
    assert resolve(document, at=30, cutoff=50)["instrument"]["id"] == "equity:second"


def test_overlapping_bindings_do_not_silently_pick_the_later_known_instrument():
    document = registry()
    document["instruments"].append(instrument("equity:second"))
    document["aliases"].append(alias(identifier="equity:second", listing_id=None, known_at=30))
    assert resolve(document, cutoff=29)["instrument"]["id"] == "equity:abc"
    with pytest.raises(AmbiguousAliasError):
        resolve(document, cutoff=30)


def test_reference_fund_and_future_are_distinct_and_isin_is_optional():
    document = {"schema": "market-registry.v1", "instruments": [
        instrument("gold:reference", "commodity", "reference"),
        instrument("gold:fund", "commodity", "fund"),
        {**instrument("gold:future:2027", "commodity", "future"),
         "contract": {"expiry": 1800000000, "unit": "troy ounce", "multiplier": 100,
                      "settlement": "physical", "underlying_instrument_id": "gold:reference"}},
    ], "listings": [], "aliases": [alias(identifier="gold:reference", listing_id=None)]}
    assert resolve(document)["listing"] is None
    assert len(validate_registry(document)["instruments"]) == 3
    document["listings"].append(listing(instrument_id="gold:reference"))
    with pytest.raises(IdentityError, match="references"):
        validate_registry(document)


def test_unannounced_instruments_do_not_enter_historical_view():
    document = registry()
    document["instruments"].append(instrument("equity:future"))
    document["aliases"].append(alias(identifier="equity:future", listing_id=None, known_at=40))
    assert [row["id"] for row in registry_view(document, as_of=30, effective_at=20)["instruments"]] == ["equity:abc"]


def test_validation_and_queries_return_independent_defensive_copies():
    document = registry()
    validated = validate_registry(document)
    validated["instruments"][0]["name"] = "changed"
    result = resolve(document)
    result["instrument"]["name"] = "changed again"
    result["listing"]["native_currency"] = "EUR"
    assert document == registry()


@pytest.mark.parametrize("timestamp", [True, False, 12.5, "2026-01-01T00:00:00Z", -1])
def test_invalid_timestamp_types_are_rejected(timestamp):
    document = registry()
    document["aliases"][0]["known_at"] = timestamp
    with pytest.raises(IdentityError, match="integer UTC"):
        validate_registry(document)
    with pytest.raises(IdentityError, match="integer UTC"):
        resolve(registry(), cutoff=timestamp)


@pytest.mark.parametrize("mutation", [
    lambda document: document.update(unknown=True),
    lambda document: document["instruments"][0].update(ticker="ABC"),
    lambda document: document["listings"][0].update(instrument_id="missing"),
    lambda document: document["aliases"][0].update(listing_id="missing"),
    lambda document: document["aliases"][0].update(effective_until=10),
    lambda document: document["aliases"].append(copy.deepcopy(document["aliases"][0])),
    lambda document: document["listings"].append(listing(known_at=30, native_currency="EUR")),
    lambda document: document["instruments"].append(instrument()),
])
def test_unknown_fields_broken_references_duplicate_revisions_and_identity_changes_fail(mutation):
    document = registry()
    mutation(document)
    with pytest.raises(IdentityError):
        validate_registry(document)


def test_registry_capacity_is_bounded_but_accepts_one_thousand_identities():
    document = {"schema": "market-registry.v1", "instruments": [instrument(f"test:{index}") for index in range(1000)],
                "listings": [], "aliases": []}
    assert len(validate_registry(document)["instruments"]) == 1000
    document["instruments"].append(instrument("test:1000"))
    with pytest.raises(IdentityError, match="1000"):
        validate_registry(document)


def test_legacy_projection_preserves_original_selectors_and_hashes():
    original = {"source": "aggregate:publisher", "asset": "BTC", "quote": "EUR", "price": 100, "observed_at": 1000}
    serialized = json.dumps(original, sort_keys=True).encode()
    before_hash = hashlib.sha256(serialized).hexdigest()
    projection = legacy_crypto_identity(original["asset"], original["quote"], original["source"])
    assert projection == legacy_crypto_identity("BTC", "EUR", "aggregate:publisher")
    assert projection["listing_id"] is None
    assert projection["legacy"] == {"asset": "BTC", "quote": "EUR", "source": "aggregate:publisher"}
    assert projection["instrument_id"] == legacy_crypto_identity("BTC", "USD", "other")["instrument_id"]
    assert projection["legacy_key_sha256"] != legacy_crypto_identity("BTC", "USD", "other")["legacy_key_sha256"]
    assert projection["instrument_id"] != legacy_crypto_identity("btc", "EUR", "aggregate:publisher")["instrument_id"]
    assert hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest() == before_hash
