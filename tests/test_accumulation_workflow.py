import json
import time

import pytest

from crypto_market_analysis.cli import main
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle

DAY = 86400


@pytest.fixture
def market(tmp_path):
    end = int(time.time()) // 3600 * 3600
    start = end - 200 * DAY
    engine = Engine(Settings(data_dir=tmp_path), "demo")
    rows = [Candle("fixture", "BTC", "EUR", "1h", start + hour * 3600,
                   start + (hour + 1) * 3600, 100, 101, 99, 100, 1,
                   start + (hour + 1) * 3600, data_mode="demo") for hour in range(200 * 24)]
    engine.store.put_candles(rows)
    config = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "fixture",
              "data_mode": "demo", "start_time": end - 180 * DAY,
              "contributions": [{"time": end - 180 * DAY, "amount": "100"}],
              "schedule": [{"time": end - 180 * DAY, "amount": "90"}],
              "fees": {"fixed": "1", "rate": "0", "asset": "quote"}}
    return engine, config, end


def test_real_engine_context_windows_and_accounting_share_native_identity(market):
    engine, config, end = market
    context = engine.context("BTC", "fixture", "EUR", end)
    assert context["market"]["quote"] == "EUR"
    assert len(context["price_history"]) >= 199
    assert context["lookbacks"]["730"]["return"]["value"] is None
    windows = engine.purchase_windows("BTC", "fixture", "EUR", end, [1], [0.05])
    assert windows["anchor_price"] == 100
    assert windows["windows"]["1"]["0.05"]["effective_samples"] >= 20
    plans = engine.compare_plans(config, end)
    regular = plans["plans"]["regular"]
    assert regular["reserve"] == "9"
    assert regular["net_quantity"] == "0.9"
    assert regular["terminals"]["180"]["total_wealth"] == "99"
    assert plans["input_reference"]["quote"] == "EUR"
    assert engine.context("BTC", "fixture", "USD", end)["status"] == "unavailable"


def test_actual_issuance_retries_and_outcomes_preserve_original(market):
    engine, config, end = market
    first = engine.issue_assessment("test-local", "BTC", "fixture", "EUR", end, config)
    assert first["issued_at"] >= end
    assert first["payload"]["plan"]["plans"]["regular"]["reserve"] == "9"
    assert engine.issue_assessment("test-local", "BTC", "fixture", "EUR", end, config) == first
    with pytest.raises(ValueError, match="different request"):
        engine.issue_assessment("test-local", "BTC", "fixture", "USD", end, config)
    outcome = engine.observe_assessment(first["assessment_id"])
    assert len(outcome["payload"]["windows"]) == 12
    assert engine.assessment(first["assessment_id"]) == first
    assert len(engine.assessment_outcomes(first["assessment_id"])) == 1


def test_readonly_calculations_do_not_create_journal_or_watch_tables(market):
    engine, _, end = market
    engine.context("BTC", "fixture", "EUR", end)
    result = engine.vigilance("BTC", "fixture", "EUR", end)
    assert result["quality"] == "degraded"
    with engine.store.connection() as con:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "assessment_journal" not in names
    assert "vigilance_states" not in names


def test_real_cli_reports_and_plan_chart(tmp_path, capsys, market):
    engine, config, end = market
    file = tmp_path / "plan.json"
    file.write_text(json.dumps(config))
    common = ["--data-dir", str(engine.settings.data_dir), "--as-of", str(end), "--mode", "demo"]
    assert main(["compare-plans", *common, "--plan-config", str(file)]) == 0
    assert json.loads(capsys.readouterr().out)["plans"]["regular"]["reserve"] == "9"
    assert main(["context", *common, "--source", "fixture", "--quote", "EUR", "--format", "markdown"]) == 0
    report = capsys.readouterr().out
    assert "EUR" in report and "fixture" in report
    output = tmp_path / "plan.svg"
    assert main(["chart", *common, "--kind", "plans", "--plan-config", str(file), "--output", str(output)]) == 0
    capsys.readouterr()
    svg = output.read_text()
    assert "reserve" in svg.lower() and "EUR" in svg


def test_calendar_import_is_idempotent_and_included_in_captured_evidence(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    calendar = tmp_path / "cpi.ics"
    calendar.write_text("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:synthetic-cpi\n"
                        "SUMMARY:Consumer Price Index\nDTSTART;TZID=America/New_York:20260312T083000\n"
                        "END:VEVENT\nEND:VCALENDAR\n")
    assert engine.import_calendar(calendar)["inserted"] == 1
    assert engine.import_calendar(calendar)["inserted"] == 0
    bundle = engine.bundle(include_forecast=False)
    assert len(bundle["calendar"]) == 1
    assert bundle["calendar"][0]["publication_time_verified"] is False
