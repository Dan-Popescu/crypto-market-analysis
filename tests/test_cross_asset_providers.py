"""Offline, synthetic provider contracts; no market-data fixtures or live calls."""

import copy

import httpx
import pytest

from crypto_market_analysis.cross_asset.providers import fetch_series, provider_catalog
from crypto_market_analysis.http_limits import HTTPPolicy, http_budget_scope


def descriptor(provider="normalized_import"):
    return {"id": "synthetic-equity-raw", "instrument_id": "synthetic-equity", "listing_id": "synthetic-xnys",
            "provider": provider, "provider_symbol": "SYNTH", "native_currency": "USD", "unit": "share",
            "price_basis": "raw", "data_mode": "import", "calendar_id": "synthetic-calendar",
            "calendar_version": "v1", "interval": "session", "revision_semantics": "captured_history"}


def imported():
    return {"descriptor": descriptor(), "start": -100, "end": 200,
            "source_contract": {"origin": "https://example.com/synthetic-source", "rights": "synthetic test data",
                                "price_basis": "raw", "action_coverage": "complete_for_requested_window"},
            "import_data": {"bars": [{"start": -10, "end": 100, "open": 10, "high": 12, "low": 9, "close": 11,
                                      "observed_at": 101}],
                            "actions": [{"id": "split1", "kind": "split", "effective_at": 0,
                                         "announced_at": -50, "observed_at": -40, "ratio": 2}]}}


def fred():
    request = {"descriptor": descriptor("fred_graph"), "start": 0, "end": 200000,
               "opt_in_network": True, "publication_grid": [
                   {"session_date": "1970-01-01", "start": 0, "end": 86400},
                   {"session_date": "1970-01-02", "start": 86400, "end": 172800}]}
    request["descriptor"].update(provider_symbol="DCOILWTICO", native_currency="USD", unit="USD_per_barrel",
                                 listing_id=None, interval="reference", data_mode="live")
    return request


def test_catalogue_has_no_default_or_unearned_qualification():
    first = provider_catalog()
    assert first["default_provider"] is None
    assert not any(row["production_accepted"] for row in first["providers"])
    first["providers"].clear()
    assert provider_catalog()["providers"]


def test_import_preserves_native_prices_actions_and_actual_receipt_without_http():
    request = imported()
    original = copy.deepcopy(request)
    result = fetch_series(request, received_at=300)
    assert request == original
    assert result["series"]["bars"][0]["close"] == 11
    assert result["series"]["bars"][0]["observed_at"] == 300
    assert result["series"]["actions"][0]["observed_at"] == 300
    assert result["series"]["actions"][0]["announced_at"] == -50
    assert result["series"]["descriptor"]["rights"] == "synthetic test data"
    assert result["http_accounting"]["totals"]["requests"] == 0
    assert result["source_assessment"]["rights_verified"] is False


def test_normalized_csv_accepts_actions_sidecar_and_does_not_ignore_action_columns():
    request = imported()
    actions = request.pop("import_data")["actions"]
    request["import_csv"] = "start,end,open,high,low,close\n-10,100,10,12,9,11\n"
    request["import_actions"] = actions
    result = fetch_series(request, received_at=300)
    assert result["series"]["actions"][0]["ratio"] == 2
    request["import_csv"] = "start,end,open,high,low,close,dividend\n-10,100,10,12,9,11,1\n"
    with pytest.raises(ValueError, match="Normalized CSV"):
        fetch_series(request, received_at=300)


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("source_contract"),
    lambda r: r["descriptor"].update(data_mode="live"),
    lambda r: r["descriptor"].update(revision_semantics="point_in_time"),
    lambda r: r["source_contract"].update(price_basis="split_adjusted"),
    lambda r: r["limits"].update(max_bytes=True),
    lambda r: r["limits"].update(max_rows=1),
    lambda r: r["import_data"]["bars"][0].update(close=float("nan")),
])
def test_import_contracts_fail_closed(mutation):
    request = imported()
    request["limits"] = {}
    mutation(request)
    with pytest.raises(ValueError):
        fetch_series(request, received_at=300)


def test_byte_limit_includes_action_sidecar():
    request = imported()
    request["limits"] = {"max_bytes": 10}
    with pytest.raises(ValueError, match="byte limit"):
        fetch_series(request, received_at=300)


def test_fred_native_reference_negative_values_missing_and_real_accounting():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "fred.stlouisfed.org"
        assert request.url.params["id"] == "DCOILWTICO"
        return httpx.Response(200, text="observation_date,DCOILWTICO\n1970-01-01,-4\n1970-01-02,.\n")

    result = fetch_series(fred(), received_at=200000, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result["series"]["bars"][0]["close"] == -4
    assert result["series"]["bars"][0]["start"] == 0
    assert result["metrics"]["missing_dates"] == ["1970-01-02"]
    assert result["http_accounting"]["totals"]["requests"] == 1
    assert result["http_accounting"]["totals"]["decoded_body_bytes"] > 0
    assert result["source_assessment"]["executable"] is False
    assert result["source_assessment"]["production_accepted"] is False


def test_network_optin_and_reference_identity_required_before_connection():
    request = fred()
    request.pop("opt_in_network")
    with pytest.raises(ValueError, match="opt_in_network"):
        fetch_series(request)
    request["opt_in_network"] = True
    request["descriptor"]["listing_id"] = "a-stock-listing"
    with pytest.raises(ValueError, match="never a traded listing"):
        fetch_series(request)


def test_fred_publication_points_preserve_exact_calendar_bounds():
    request = fred()
    request["publication_grid"] = [{"session_date": "1970-01-01", "start": 12345, "end": 12345}]
    result = fetch_series(request, received_at=200000, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="DATE,DCOILWTICO\n1970-01-01,4\n")))
    assert result["status"] == "ok"
    assert result["series"]["bars"][0]["start"] == result["series"]["bars"][0]["end"] == 12345


@pytest.mark.parametrize("code,attempts,status", [(503, 2, "unavailable"), (429, 1, "deferred"), (302, 1, "unavailable")])
def test_bounded_retry_no_redirects_or_rate_limit_loop(code, attempts, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, text="synthetic failure", headers={"location": "https://example.com/"})

    result = fetch_series(fred(), received_at=200000, transport=httpx.MockTransport(handler))
    assert len(calls) == attempts
    assert result["status"] == status
    assert result["http_accounting"]["totals"]["requests"] == attempts
    assert result["http_accounting"]["totals"]["http_errors"] == (attempts if code >= 400 else 0)


def test_connection_failure_counted_and_error_does_not_disclose_url():
    def handler(request):
        raise httpx.ConnectError("secret URL example", request=request)

    result = fetch_series(fred(), transport=httpx.MockTransport(handler))
    assert result["error"] == "ConnectError"
    assert result["http_accounting"]["totals"]["requests"] == 2
    assert result["http_accounting"]["totals"]["errors"] == 2


def test_outer_budget_is_shared_not_reset_for_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="temporary")

    with http_budget_scope(HTTPPolicy(1, 30)):
        result = fetch_series(fred(), transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result["http_accounting"]["http_limits"]["used_requests"] == 1
    assert "request_limit" in result["error"]


@pytest.mark.parametrize("body", ["<html>blocked</html>", "DATE,DCOILWTICO\n1970-01-01,nan\n",
                                  "DATE,DCOILWTICO\n1970-01-01,1\n1970-01-01,2\n"])
def test_bad_csv_is_unavailable(body):
    result = fetch_series(fred(), transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    assert result["status"] == "unavailable"
    assert result["series"] is None


def test_response_byte_cap_and_row_cap():
    request = fred()
    request["limits"] = {"max_bytes": 10}
    result = fetch_series(request, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 11)))
    assert result["series"] is None
    assert "byte limit" in result["error"]
    assert result["http_accounting"]["totals"]["decoded_body_bytes"] == 11


def test_row_cap_counts_unrequested_provider_rows_and_absent_grid_dates_remain_missing():
    request = fred()
    request["publication_grid"] = request["publication_grid"][:1]
    request["limits"] = {"max_rows": 1}
    body = "DATE,DCOILWTICO\n1969-12-31,1\n1970-01-01,2\n"
    result = fetch_series(request, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    assert result["status"] == "unavailable"
    empty = fetch_series(fred(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="DATE,DCOILWTICO\n")))
    assert empty["metrics"]["missing_dates"] == ["1970-01-01", "1970-01-02"]


def test_upstream_bad_value_never_appears_in_error():
    result = fetch_series(fred(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="DATE,DCOILWTICO\n1970-01-01,private-source-text\n")))
    assert result["status"] == "unavailable"
    assert "private-source-text" not in result["error"]


def test_cooperative_deadline_checked_after_transport(monkeypatch):
    from crypto_market_analysis.cross_asset import providers

    clock = [0.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])

    def handler(request):
        clock[0] = 40.0
        return httpx.Response(200, text="")

    result = fetch_series(fred(), transport=httpx.MockTransport(handler))
    assert result["series"] is None
    assert "deadline" in result["error"]
    assert result["http_accounting"]["totals"]["requests"] == 1


def test_provider_never_calls_unqualified_yahoo_or_stooq():
    for provider in ("yahoo_chart", "stooq_csv"):
        request = imported()
        request["descriptor"]["provider"] = provider
        with pytest.raises(ValueError, match="unsupported or unqualified"):
            fetch_series(request)
