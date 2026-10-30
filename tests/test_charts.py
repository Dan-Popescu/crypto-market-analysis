from pathlib import Path

import pytest

from crypto_market_analysis.charts import _complete_daily, render_chart
from crypto_market_analysis.models import Candle, Observation

AS_OF = 1_735_776_000


def candles(days=1, start=AS_OF - 86400, mode="live"):
    return [
        Candle("kraken", "BTC", "USD", "1h", t, t + 3600, 100, 102, 99, 101, 2, t + 3600, data_mode=mode)
        for t in range(start, start + days * 86400, 3600)
    ]


def obs(metric, unit, value=1.0, **kwargs):
    return Observation("kraken", "BTC", metric, AS_OF - 100, AS_OF - 50, value, unit, "USD", kwargs)


@pytest.mark.parametrize("ext", ["png", "svg"])
def test_price_renders_and_labels_source_quote(tmp_path: Path, ext: str):
    output = tmp_path / f"chart.{ext}"
    assert render_chart("price", candles(), [], output, AS_OF) == output
    assert output.stat().st_size > 1000
    if ext == "svg":
        text = output.read_text()
        assert "kraken" in text and "BTC/USD" in text and "UTC" in text


def test_depth_incomplete_is_labelled_and_native_quotes_remain_separate(tmp_path):
    rows = [
        obs("depth_bid_1pct", "USD", coverage_complete=False),
        Observation(
            "other",
            "BTC",
            "depth_ask_1pct",
            AS_OF - 100,
            AS_OF - 50,
            50,
            "EUR",
            "EUR",
            {"coverage_complete": True},
        ),
    ]
    output = render_chart("liquidity", [], rows, tmp_path / "book.svg", AS_OF)
    text = output.read_text()
    assert "lower bound" in text and "USD" in text and "EUR" in text


def test_calibration_uses_empirical_bins(tmp_path):
    evaluation = {
        "horizon": 7,
        "source": "kraken",
        "quote": "USD",
        "data_mode": "demo",
        "metrics": {
            "red": {"calibration_bins": [{"n": 5, "mean_probability": 0.4, "observed_frequency": 0.6}]}
        },
    }
    result = render_chart("calibration", [], [], tmp_path / "cal.svg", AS_OF, evaluation=evaluation)
    text = result.read_text()
    assert "n=5" in text and "kraken" in text and "DEMONSTRATION" in text


@pytest.mark.parametrize("kind", ["price", "weekly", "liquidity", "leverage", "options", "calibration"])
def test_missing_data_fails(kind, tmp_path):
    with pytest.raises(ValueError, match="unavailable"):
        render_chart(kind, [], [], tmp_path / "empty.png", AS_OF)


def test_future_observations_not_drawn(tmp_path):
    row = Observation("kraken", "BTC", "funding_rate_actual", AS_OF + 1, AS_OF, 0.01, "rate")
    with pytest.raises(ValueError, match="unavailable"):
        render_chart("leverage", [], [row], tmp_path / "future.svg", AS_OF)


def test_options_need_actual_expiry(tmp_path):
    row = obs("option_atm_iv_7d", "percent", 45)
    with pytest.raises(ValueError, match="unavailable"):
        render_chart("options", [], [row], tmp_path / "options.svg", AS_OF)


def test_incomplete_daily_bar_is_excluded():
    rows = candles(days=2, start=AS_OF - 2 * 86400)
    rows.pop(4)
    _, daily = _complete_daily(rows, AS_OF)
    assert len(daily) == 1
    assert int(daily.index[0].timestamp()) == AS_OF - 86400


def test_weekly_excludes_incomplete_week(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    monday = 1_735_689_600 - 2 * 86400  # 2024-12-30
    rows = candles(days=14, start=monday)
    rows.pop(8 * 24 + 3)
    count = []
    original = Axes.add_patch

    def add_patch(self, patch):
        count.append((self, patch))
        return original(self, patch)

    monkeypatch.setattr(Axes, "add_patch", add_patch)
    render_chart("weekly", rows, [], tmp_path / "weekly.svg", monday + 14 * 86400)
    assert len([patch for ax, patch in count if ax.get_ylabel() == "Price (USD)"]) == 1


def test_funding_periods_are_separate(tmp_path):
    rows = [
        obs("funding_rate_actual", "rate", funding_interval_hours=1),
        obs("funding_rate_actual", "rate", funding_interval_hours=8),
    ]
    output = render_chart("leverage", [], rows, tmp_path / "funding.svg", AS_OF)
    text = output.read_text()
    assert "Funding · 1 h" in text and "Funding · 8 h" in text
