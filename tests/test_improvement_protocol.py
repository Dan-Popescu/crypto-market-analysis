from crypto_market_analysis.models import Candle
from crypto_market_analysis.purchase_windows import resolve_window


def test_off_hour_observable_mark_preserves_unknown_exact_endpoint():
    rows = [Candle("fixture", "BTC", "EUR", "1h", hour * 3600, (hour + 1) * 3600,
                   100, 100, 90, 90, 1, (hour + 1) * 3600) for hour in range(26)]
    result = resolve_window(rows, 100, 0, 1800, 1, .05, 26 * 3600,
                            endpoint_convention="hourly_after_deadline_v1")
    assert result["endpoint_below_anchor"]["state"] == "ambiguous"
    mark = result["observable_endpoint"]
    assert mark["state"] == "known_true"
    assert mark["offset_seconds"] == 1800
    assert mark["mark_time"] == "1970-01-02T01:00:00+00:00"
    assert mark["forecast_comparability"] == "not_established_for_anchor_relative_estimates"
