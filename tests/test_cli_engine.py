import json

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.demo import seed_demo
from crypto_market_analysis.engine import Engine


def test_empty_live_does_not_fall_back_to_demo(tmp_path):
    settings = Settings(data_dir=tmp_path)
    engine = Engine(settings)
    seed_demo(engine.store, days=90, now=1793145600)
    live = engine.bundle("BTC", as_of=1793145600)
    assert live["selected_market"] is None
    assert live["forecast"]["horizons"]["7"]["probabilities"] is None
    demo = Engine(settings, "demo").bundle("BTC", as_of=1793145600)
    assert demo["data_mode"] == "demo"
    assert demo["analysis"]["coverage"]["complete_days"] == 90


def test_cli_demo_report_chart_and_invalid_horizon(tmp_path, capsys):
    directory = str(tmp_path / "data")
    assert main(["demo", "--data-dir", directory, "--days", "90"]) == 0
    capsys.readouterr()
    assert main(["brief", "--data-dir", directory, "--mode", "demo", "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data_mode"] == "demo"
    assert result["selected_market"] == {"source": "demo", "quote": "USD"}
    assert result["analysis"]["coverage"]["complete_days"] == 90
    output = tmp_path / "weekly.png"
    assert (
        main(
            ["chart", "--data-dir", directory, "--mode", "demo", "--kind", "weekly", "--output", str(output)]
        )
        == 0
    )
    assert output.read_bytes().startswith(b"\x89PNG")
    capsys.readouterr()
    assert main(["forecast", "--data-dir", directory, "--horizons", "61"]) == 2
    assert "horizons" in capsys.readouterr().err


def test_event_import_records_availability_and_source(tmp_path, capsys):
    events = tmp_path / "events.json"
    events.write_text(
        json.dumps(
            [
                {
                    "title": "FOMC announcement",
                    "event_time": "2030-01-01T12:00:00Z",
                    "source_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                }
            ]
        )
    )
    assert main(["import-events", "--data-dir", str(tmp_path / "data"), "--file", str(events)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["inserted"] == 1
    assert result["origin"] == "manual_import"


def test_configuration_relative_directory_and_bad_venue(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('data_dir="nearby"\nvenues=["kraken"]\n')
    assert main(["status", "--config", str(config)]) == 0
    capsys.readouterr()
    assert (tmp_path / "nearby" / "observer.sqlite").exists()
    assert main(["collect", "--data-dir", str(tmp_path / "data"), "--venues", "unknown"]) == 2
    assert "Unknown venue" in capsys.readouterr().err


def test_cli_help_report_and_errors_are_english(tmp_path, capsys):
    from crypto_market_analysis.cli import parser
    from crypto_market_analysis.engine import render_brief

    help_text = parser().format_help()
    assert "BTC/ETH analysis using free public data." in help_text
    assert "English report or structured dossier for AI." in help_text
    engine = Engine(Settings(data_dir=tmp_path))
    report = render_brief(engine.bundle("BTC", as_of=1793145600))
    assert report.startswith("# BTC — daily report\nAs of ")
    assert "Insufficient price data: collect or import completed hourly candles." in report
    assert "## Experimental scenarios" in report
    assert "## Coverage and limitations" in report
    assert main(["evaluate", "--data-dir", str(tmp_path), "--horizon", "61"]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "error": "The horizon must be between 1 and 60 days.",
        "hint": "Use crypto-market-analysis <command> --help.",
    }
