"""Synthetic application acceptance: source qualification remains separate."""

import copy

import pytest

from crypto_market_analysis.cross_asset import application


def bundle():
    registry = {"schema": "market-registry.v1", "instruments": [
        {"id": "equity:example", "name": "Synthetic share", "asset_class": "equity", "form": "share"}],
        "listings": [{"id": "listing:usd", "instrument_id": "equity:example", "venue_mic": "XNYS",
                      "native_currency": "USD", "calendar_id": "calendar:test", "active_from": 0,
                      "active_until": None, "known_at": 0}],
        "aliases": [{"provider": "test", "symbol": "EXAMPLE", "instrument_id": "equity:example",
                     "listing_id": "listing:usd", "effective_from": 0, "effective_until": None, "known_at": 0}]}
    calendar = {"schema": "market-calendar.v1", "id": "calendar:test", "version": "v1", "timezone": "UTC",
                "source_url": "https://example.test/schedule", "known_at": 0, "supported_start": 0,
                "supported_end": 200000, "profile": "explicit", "sessions": [
                    {"date": "1970-01-01", "segments": [{"start": 10, "end": 40}]},
                    {"date": "1970-01-02", "segments": [{"start": 86410, "end": 86440}]}]}
    series = {"schema": "market-series.v1", "descriptor": {
        "id": "series:test", "instrument_id": "equity:example", "listing_id": "listing:usd", "provider": "test",
        "provider_symbol": "EXAMPLE", "native_currency": "USD", "unit": "share", "price_basis": "raw",
        "data_mode": "demo", "calendar_id": "calendar:test", "calendar_version": "v1", "interval": "session",
        "revision_semantics": "point_in_time", "source_url": "https://example.test/prices", "rights": "Synthetic fixture",
        "action_coverage": {"start": 0, "end": 200000, "known_at": 0, "status": "declared_complete",
                            "kinds": ["split", "cash_dividend"], "source_url": "https://example.test/actions"}},
        "bars": [{"start": start, "end": end, "open": price, "high": price, "low": price, "close": price,
                  "observed_at": end, "session_date": date}
                 for start, end, price, date in [(10, 40, 10, "1970-01-01"), (86410, 86440, 11, "1970-01-02")]],
        "actions": []}
    return {"schema": "market-bundle.v1", "registry": registry, "calendars": [calendar], "series": [series]}


class MemoryStore:
    def __init__(self, document=None):
        self.document = application.validate_bundle(document or bundle())
        self.requests = []

    def registry(self, *, as_of):
        return copy.deepcopy(self.document["registry"])

    def calendar(self, calendar_id, version, *, as_of):
        return copy.deepcopy(next((row for row in self.document["calendars"] if
                                   row["id"] == calendar_id and row["version"] == version and row["known_at"] <= as_of), None))

    def read_series(self, series_id, *, as_of, data_mode, start, end, max_rows):
        self.requests.append((series_id, data_mode, as_of, start, end, max_rows))
        result = next((row for row in self.document["series"] if
                       row["descriptor"]["id"] == series_id and row["descriptor"]["data_mode"] == data_mode), None)
        if result is None:
            return None
        result = copy.deepcopy(result)
        result["bars"] = [row for row in result["bars"] if start <= row["start"] and row["end"] <= end
                          and row["observed_at"] <= as_of]
        return result


def request(**changes):
    return {"series_id": "series:test", "data_mode": "demo", "currency": "USD", "start": 0, "end": 100000,
            "annualization": {"periods_per_year": 252, "convention": "synthetic daily sessions"}, **changes}


def plan_request(**changes):
    result = request(**changes)
    result.pop("annualization")
    result["plan"] = {"currency": "USD", "initial_cash": 100, "deposits": [],
                      "purchases": [{"at": 40, "cash_amount": 50}], "fee_per_trade": 0,
                      "distributions": "cash", "actions_complete": True}
    return result


def test_capabilities_disclose_implemented_semantics_and_unqualified_sources():
    result = application.market_capabilities()
    assert result["qualification"]["synthetic_acceptance"] is True
    assert result["qualification"]["qualified_live_sources"] == []
    for feature in ("futures_cash_plan", "reference_cash_fills", "continuous_cash_fills", "currency_conversion"):
        assert result["features"][feature]["supported"] is False


def test_bundle_checks_listing_alias_currency_calendar_and_native_contracts():
    original = bundle()
    validated = application.validate_bundle(original)
    validated["registry"]["instruments"][0]["name"] = "changed"
    assert original["registry"]["instruments"][0]["name"] == "Synthetic share"
    for field, value in (("native_currency", "EUR"), ("provider_symbol", "UNKNOWN"),
                         ("calendar_version", "v9"), ("listing_id", None), ("price_basis", "continuous")):
        document = bundle()
        document["series"][0]["descriptor"][field] = value
        with pytest.raises(ValueError):
            application.validate_bundle(document)


def test_bundle_revision_shortening_is_applied_before_effective_filtering():
    document = bundle()
    document["registry"]["aliases"].append({**document["registry"]["aliases"][0],
                                            "effective_until": 86400, "known_at": 100})
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        application.validate_bundle(document)


def test_bundle_validates_registry_once_for_many_series(monkeypatch):
    document = bundle()
    document["series"] = [{**copy.deepcopy(document["series"][0]), "descriptor": {
        **document["series"][0]["descriptor"], "id": f"series:{index}"}} for index in range(1000)]
    calls = []
    original = application.identity.validate_registry

    def counted(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(application.identity, "validate_registry", counted)
    assert len(application.validate_bundle(document)["series"]) == 1000
    assert len(calls) == 1


def test_analysis_keeps_metadata_native_prices_and_session_coverage():
    store = MemoryStore()
    result = application.market_analysis(store, request(), as_of=100000)
    assert result["status"] == "ok", result
    assert result["analysis"]["simple_return"] == pytest.approx(0.1)
    assert result["descriptor"]["native_currency"] == "USD"
    assert result["identity"]["listing"]["id"] == "listing:usd"
    assert result["calendar"]["source_url"] == "https://example.test/schedule"
    assert result["coverage"]["counts"]["observed"] == 2
    assert result["coverage"]["closed_intervals"]
    assert store.requests == [("series:test", "demo", 100000, 0, 100000, 10000)]


def test_empty_closed_window_preserves_coverage_and_missing_or_due_states():
    store = MemoryStore()
    closed = application.market_analysis(store, request(start=50, end=100), as_of=100)
    assert closed["status"] == "ok", closed
    assert closed["analysis"]["status"] == "insufficient_data"
    assert closed["coverage"]["closed_seconds"] == 50
    due = application.market_analysis(store, request(), as_of=20)
    assert due["coverage"]["counts"]["not_yet_due"] == 2
    store.document["series"][0]["bars"] = []
    missing = application.market_analysis(store, request(), as_of=100000)
    assert missing["coverage"]["counts"]["missing"] == 2


def test_mode_currency_unknown_fields_and_unbounded_clock_requests_fail_explicitly():
    store = MemoryStore()
    assert application.market_analysis(store, request(data_mode="import"), as_of=100000)["status"] == "unavailable"
    for changes in ({"currency": "EUR"}, {"unknown": 1}, {"start": True}, {"end": 10**15}, {"data_mode": "mixed"}):
        assert application.market_analysis(store, request(**changes), as_of=100000)["status"] == "invalid_request"


def test_registry_alias_and_calendar_operations_are_bounded_and_inspectable():
    store = MemoryStore()
    result = application.market_registry(store, {"provider": "test", "symbol": "EXAMPLE", "effective_at": 10}, as_of=100)
    assert result["registry"]["instrument"]["id"] == "equity:example"
    calendar = application.market_calendar(store, {"calendar_id": "calendar:test", "calendar_version": "v1",
                                                   "start": 0, "end": 100}, as_of=100)
    assert calendar["coverage"]["counts"]["missing"] == 1
    assert application.market_registry(store, {"provider": "test"}, as_of=100)["status"] == "invalid_request"


def test_cash_plan_uses_native_raw_prices_and_explicit_historical_event_window():
    store = MemoryStore()
    result = application.market_plan(store, plan_request(), as_of=100000)
    assert result["status"] == "ok", result
    assert result["plan"]["currency"] == "USD"
    changed = plan_request(start=50)
    assert application.market_plan(store, changed, as_of=100000)["status"] == "invalid_request"
    changed = plan_request()
    changed["plan"]["currency"] = "EUR"
    assert application.market_plan(store, changed, as_of=100000)["status"] == "invalid_request"


def test_adjusted_and_reference_series_cannot_supply_cash_fills():
    document = bundle()
    document["series"][0]["descriptor"]["price_basis"] = "split_adjusted"
    result = application.market_plan(MemoryStore(document), plan_request(), as_of=100000)
    assert result["status"] == "unsupported"
    assert result["descriptor"]["price_basis"] == "split_adjusted"
    document = bundle()
    document["registry"]["instruments"][0]["form"] = "reference"
    document["registry"]["listings"] = []
    document["registry"]["aliases"][0]["listing_id"] = None
    document["series"][0]["descriptor"].update(listing_id=None, interval="reference")
    for session in document["calendars"][0]["sessions"]:
        session["publication_at"] = session["segments"][-1]["end"]
    result = application.market_plan(MemoryStore(document), plan_request(), as_of=100000)
    assert result["status"] == "unsupported"


def test_bundle_rejects_closed_or_misaligned_native_intervals():
    document = bundle()
    document["series"][0]["bars"][0]["start"] = 11
    with pytest.raises(ValueError, match="native calendar grid"):
        application.validate_bundle(document)


def test_receipt_provenance_reaches_research_and_prevents_earlier_historical_fill():
    from crypto_market_analysis.evidence import content_hash

    class DelayedStore(MemoryStore):
        def read_series(self, *args, **kwargs):
            result = super().read_series(*args, **kwargs)
            result["storage_provenance"] = {
                "descriptor_hash": content_hash(result["descriptor"]), "registry_hash": content_hash(self.document["registry"]),
                "receipt_evidence": "locally_witnessed", "actions": [], "bars": [
                    {"hash": content_hash(row), "declared_observed_at": row["observed_at"],
                     "received_at": max(row["observed_at"], 100), "effective_known_at": max(row["observed_at"], 100)}
                    for row in result["bars"]]}
            return result

    store = DelayedStore()
    result = application.market_analysis(store, request(), as_of=100000)
    assert result["status"] == "ok", result
    assert result["series"]["storage_provenance"] == result["storage_provenance"]
    plan = application.market_plan(store, plan_request(), as_of=100000)
    assert plan["status"] == "invalid_request", plan
