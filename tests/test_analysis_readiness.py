"""Readiness follows actual calculator support and one physical known-at view."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from crypto_market_analysis.analysis_readiness import (
    build_analysis_readiness,
    render_analysis_readiness_markdown,
)
from crypto_market_analysis.context import market_context
from crypto_market_analysis.evidence import content_hash
from crypto_market_analysis.models import Candle
from crypto_market_analysis.purchase_windows import analyze_windows
from crypto_market_analysis.storage import Store

NOW = 1000 * 86400


def candles(days=3, source="kraken", mode="import", end=NOW):
    return [Candle(source, "BTC", "EUR", "1h", t, t + 3600, 100 + t / NOW, 102 + t / NOW,
                   98 + t / NOW, 101 + t / NOW, 1, t + 3600, data_mode=mode)
            for t in range(end - days * 86400, end, 3600)]


def request(mode="import", **changes):
    return {"dossier": {"data_mode": mode, "markets": [
        {"asset": "BTC", "source": "kraken", "quote": "EUR", "lookbacks": [2]}]},
        "qualification": {"required_history_days": 3}, **changes}


def claims(result):
    return {row["claim"]: row for row in result["markets"][0]["claims"]}


def test_explicit_missing_calendar_prevents_overall_available_claim(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    store.put_candles(candles(days=100))
    selection = request()
    selection["dossier"]["calendar"] = {"source": "calendar", "start": NOW, "end": NOW + 86400}
    result = build_analysis_readiness(store, selection, NOW)
    assert result["calendar"]["status"] == "missing"
    assert result["status"] == "incomplete"
    assert claims(result)["hourly_anchor"]["status"] == "available"


def test_complete_context_does_not_qualify_windows_rights_or_entity(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    rows = candles()
    store.put_candles(rows)
    result = build_analysis_readiness(store, request(windows={"horizons": [1], "thresholds": [0.02]}), NOW)
    found = claims(result)
    assert found["context.2d.return"]["status"] == "available"
    assert found["context.2d.return"]["details"]["coverage"]["required_closes"] == 3
    assert found["empirical_hourly_history"]["status"] == "available"
    assert found["purchase_windows.1d.0.02"]["status"] == "incomplete"
    assert found["purchase_windows.1d.0.02"]["details"]["effective_samples"] == 0
    assert found["data_reuse_permission"]["status"] == "unsupported"
    assert found["france_purchase_entity_service"]["status"] == "unsupported"
    assert result["markets"][0]["windows"] == analyze_windows(rows, NOW, horizons=[1], thresholds=[0.02])
    assert result["dossier"]["markets"][0]["market_context"]["lookbacks"] == market_context(rows, NOW, [2])["lookbacks"]
    assert result["watermark"] == result["dossier"]["watermark"]
    assert result["qualification"]["watermark"]["candles"] == result["watermark"]["candles"]
    unhashed = {k: v for k, v in result.items() if k not in ("input_hash", "snapshot_id")}
    assert result["input_hash"] == content_hash(unhashed)
    assert "confidence score" in render_analysis_readiness_markdown(result)
    assert len({(a["kind"], str(a.get("selection"))) for a in result["next_actions"]}) == len(result["next_actions"])


def test_sparse_days_long_history_and_stale_anchor_are_distinct(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    rows = candles(days=100)
    # Many retained hours do not repair a missing hour inside a requested close day.
    store.put_candles([r for r in rows if r.open_time != NOW - 2 * 86400])
    result = build_analysis_readiness(store, request(windows={"horizons": [14], "thresholds": [0.05]}), NOW)
    found = claims(result)
    assert found["hourly_anchor"]["status"] == "available"
    assert found["context.2d.return"]["status"] == "incomplete"
    assert found["context.2d.return"]["details"]["coverage"]["obtained_closes"] == 2
    assert found["purchase_windows.14d.0.05"]["status"] == "incomplete"
    assert result["qualification"]["markets"][0]["coverage"]["missing_hours"] == 1
    assert {a["kind"] for a in result["next_actions"]} >= {"recover_recent_gap", "import_archives"}
    stale = build_analysis_readiness(store, request(windows={"horizons": [1], "thresholds": [0.02]}), NOW + 7201)
    assert claims(stale)["hourly_anchor"]["status"] == "stale"
    assert claims(stale)["purchase_windows.1d.0.02"]["status"] == "stale"


def test_long_complete_history_uses_actual_supported_estimator(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    rows = candles(days=100)
    store.put_candles(rows)
    result = build_analysis_readiness(store, request(windows={"horizons": [1, 14], "thresholds": [0.02]}), NOW)
    found = claims(result)
    assert found["purchase_windows.1d.0.02"]["status"] == "available"
    assert found["purchase_windows.14d.0.02"]["status"] == "incomplete"
    assert found["purchase_windows.14d.0.02"]["details"]["required_effective_samples"] == 20


def test_demo_and_source_failure_preserve_separate_dimensions(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.put_candles(candles(mode="demo"))
    with store.connection() as con:
        con.execute("INSERT INTO runs(source,task,observed_at,payload) VALUES(?,?,?,?)",
                    ("kraken", "market", NOW, '{"status":"error","task":"market"}'))
    result = build_analysis_readiness(store, request(mode="demo"), NOW)
    found = claims(result)
    assert result["status"] == "demo_only"
    assert found["hourly_anchor"]["status"] == "demo_only"
    assert found["context.2d.return"]["status"] == "demo_only"
    assert found["source_runtime_access"]["details"]["source_health"]["latest_run"]["status"] == "error"
    assert found["source_runtime_access"]["details"]["access"]["status"] == "untested"
    assert found["data_reuse_permission"]["status"] == "unsupported"
    assert found["purchase_windows"]["status"] == "not_requested"


def test_missing_store_and_invalid_selectors_do_not_initialize(tmp_path):
    path = tmp_path / "new" / "db.sqlite"
    result = build_analysis_readiness(Store(path, initialize=False), request(), NOW)
    assert result["status"] == "missing"
    assert result["dossier"] is None
    assert not path.parent.exists()
    with pytest.raises(ValueError, match="selectors derive"):
        build_analysis_readiness(Store(path, initialize=False), request(qualification={"sources": ["other"]}), NOW)
    with pytest.raises(ValueError):
        build_analysis_readiness(Store(path, initialize=False), request(windows={"horizons": [0]}), NOW)
    with pytest.raises(ValueError):
        build_analysis_readiness(Store(path, initialize=False), request(freshness_seconds=True), NOW)
    assert not path.parent.exists()


def test_concurrent_revision_between_dossier_and_qualification_uses_same_physical_view(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    rows = candles()
    store.put_candles(rows)
    from crypto_market_analysis import analysis_readiness
    original = analysis_readiness.build_analysis_dossier
    called = False

    def after_dossier(*args, **kwargs):
        nonlocal called
        result = original(*args, **kwargs)
        if not called:
            called = True
            def write():
                Store(store.path).put_candles([replace(rows[-1], close=105, high=106, observed_at=NOW)])
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write).result(timeout=5)
        return result

    monkeypatch.setattr(analysis_readiness, "build_analysis_dossier", after_dossier)
    first = build_analysis_readiness(store, request(), NOW)
    second = build_analysis_readiness(store, request(), NOW)
    assert first["dossier"]["markets"][0]["market_context"]["anchor"]["price"] == rows[-1].close
    assert second["dossier"]["markets"][0]["market_context"]["anchor"]["price"] == 105
    assert first["qualification"]["markets"][0]["coverage"]["revision_rows"] == 0
    assert second["qualification"]["markets"][0]["coverage"]["revision_rows"] == 1
    assert first["watermark"]["candles"] == 72
    assert second["watermark"]["candles"] == 73
    assert first["qualification"]["watermark"]["candles"] == 72
    assert second["qualification"]["watermark"]["candles"] == 73
    assert first["input_hash"] != second["input_hash"]
    refs1 = first["qualification"]["markets"][0]["evidence"]["selected_rows_sha256"]
    refs2 = second["qualification"]["markets"][0]["evidence"]["selected_rows_sha256"]
    assert refs1 != refs2


def test_book_fee_derivative_identity_unknown_quote_clock_and_calendar_coverage(tmp_path):
    from crypto_market_analysis.book_snapshots import retain_books, retained_book
    from crypto_market_analysis.models import Observation

    store = Store(tmp_path / "db.sqlite")
    store.put_candles(candles())
    retain_books(store, [retained_book(
        {"timestamp": NOW * 1000, "bids": [["99", "2"]], "asks": [["101", "2"]]},
        source="kraken", asset="BTC", quote="EUR", symbol="BTC/EUR", captured_at=NOW,
        requested_limit=500, data_mode="import")])
    metadata = {"symbol": "BTC/USDT:USDT", "settle": "USDT", "market_type": "swap",
                "funding_period_seconds": 28800, "funding_type": "actual"}
    # Actual and projected rows cannot supply each other's endpoints.
    store.put_observations([
        Observation("derivative", "BTC", "funding_rate_actual", NOW, NOW, .001, "fraction", "USDT", metadata, "import"),
        Observation("derivative", "BTC", "funding_rate_projected", NOW - 14400, NOW - 14400,
                    .002, "fraction", "USDT", {**metadata, "funding_type": "projected"}, "import"),
    ])
    store.save_local_record("fx_reference", {
        "record_id": "reference", "source": "ecb", "base": "EUR", "quote": "USD", "value": "1.1",
        "event_time": None, "observed_at": NOW, "data_mode": "import", "source_url": "https://example.test/fx",
        "data_version": "synthetic-date-only", "price_type": "indicative_reference", "date": "1972-09-27"})
    store.save_local_record("calendar", {"event_id": "civil-date", "event_time": None, "local_date": "1972-09-27",
                                         "observed_at": NOW, "source": "calendar", "status": "planned"})
    wanted = request()
    wanted["dossier"]["markets"][0].update({
        "venues": {"sources": ["kraken"], "purchase_costs": {"budget": "100"}},
        "derivatives": {"source": "derivative", "quote": "USDT"}, "quotes": {"sources": ["ecb"]}})
    result = build_analysis_readiness(store, wanted, NOW)
    found = claims(result)
    assert found["venue_costs.kraken"]["status"] == "unsupported"
    assert found["venue_costs.kraken"]["details"]["hypothetical_fill"] is None
    assert "unknown_cost_and_product_rules" in found["venue_costs.kraken"]["reasons"]
    assert found["quotes.fx_reference"]["calculation_status"] == "unknown_timestamp"
    assert found["quotes.fx_reference"]["details"]["observation"]["event_time"] is None
    derivative = [c for key, c in found.items() if key.startswith("derivatives.") and key.endswith(".4h")]
    assert len(derivative) == 2
    assert all(c["status"] != "available" for c in derivative)
    assert {c["details"]["identity"]["metric"] for c in derivative} == {"funding_rate_actual", "funding_rate_projected"}
    assert result["calendar"]["details"]["coverage_status"] == "not_established_by_event_rows"
    assert result["calendar"]["status"] == "incomplete"
    assert result["calendar"]["details"]["rows"][0]["event_time"] is None
    assert any(a["kind"] == "declare_fees/product" for a in result["next_actions"])


def test_query_budget_failure_is_visible_not_silently_qualified(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.put_candles(candles())
    wanted = request()
    wanted["qualification"]["max_query_rows"] = 1
    result = build_analysis_readiness(store, wanted, NOW)
    found = claims(result)
    assert found["empirical_hourly_history"]["status"] == "unsupported"
    assert found["empirical_hourly_history"]["reasons"] == ["query_row_budget_exceeded"]
    assert found["empirical_hourly_history"]["details"]["coverage"]["calculation_truncated"] is True
    assert found["context.2d.return"]["status"] == "available"


def test_equal_vintage_derivative_conflict_is_contradictory(tmp_path):
    from crypto_market_analysis.models import Observation

    store = Store(tmp_path / "db.sqlite")
    store.put_candles(candles())
    metadata = {"symbol": "BTC/USDT:USDT", "settle": "USDT", "market_type": "swap",
                "counting_convention": "one_sided"}
    store.put_observations([
        Observation("derivative", "BTC", "open_interest_amount", NOW, NOW, value, "BTC", "USDT", metadata, "import")
        for value in (100, 200)])
    wanted = request()
    wanted["dossier"]["markets"][0]["derivatives"] = {"source": "derivative", "quote": "USDT"}
    result = build_analysis_readiness(store, wanted, NOW)
    found = claims(result)
    assert found["derivatives.0.current"]["status"] == "contradictory"
    assert found["derivatives.0.current"]["reasons"] == ["unknown_conflicting_versions"]
    assert found["derivatives.0.current"]["details"]["current"] is None
    assert len(result["dossier"]["markets"][0]["derivatives"]["stored_inputs"]["consumed_records"]) == 2


def test_window_calculation_uses_detached_inputs_after_read_session_closes(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from crypto_market_analysis import analysis_readiness

    store = Store(tmp_path / "db.sqlite")
    store.put_candles(candles())
    original_session = store.read_session
    original_windows = analysis_readiness.analyze_windows
    active = False
    calculated = False

    @contextmanager
    def tracked_session():
        nonlocal active
        with original_session() as session:
            active = True
            yield session
        active = False

    def detached_windows(rows, *args, **kwargs):
        nonlocal calculated
        if rows:  # Request validation also probes an empty input before reading.
            assert not active
            calculated = True
            # A concurrent write after capture cannot change the detached inputs.
            store.put_candles([replace(rows[-1], close=999, high=999)])
        return original_windows(rows, *args, **kwargs)

    monkeypatch.setattr(store, "read_session", tracked_session)
    monkeypatch.setattr(analysis_readiness, "analyze_windows", detached_windows)
    result = build_analysis_readiness(store, request(windows={"horizons": [1], "thresholds": [0.02]}), NOW)
    assert calculated
    captured = result["dossier"]["markets"][0]["market_context"]["stored_inputs"]["consumed_records"]
    expected = original_windows([Candle(**row) for row in captured], NOW, horizons=[1], thresholds=[0.02])
    assert result["markets"][0]["windows"] == expected
    assert result["watermark"] == result["dossier"]["watermark"]
    assert result["watermark"]["candles"] == result["qualification"]["watermark"]["candles"] == len(captured)
