from dataclasses import replace

import pytest

from crypto_market_analysis import evidence
from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.models import Candle

DAY = 86400
HOUR = 3600


def candles(start, end, price=10):
    return [Candle('fixture', 'BTC', 'EUR', '1h', time, time + HOUR,
                   price, price, price, price, 1, time + HOUR, data_mode='demo')
            for time in range(start, end, HOUR)]


@pytest.fixture
def issued(tmp_path, monkeypatch):
    # The archive helper is added by the evidence owner in the same integration wave.
    if not hasattr(evidence, 'restore_evidence'):
        monkeypatch.setattr(evidence, 'restore_evidence', lambda value: value, raising=False)
    now = [4 * DAY]
    monkeypatch.setattr('crypto_market_analysis.application.time.time', lambda: now[0])
    engine = Engine(Settings(data_dir=tmp_path), 'demo')
    original_rows = candles(2 * DAY, 4 * DAY)
    engine.store.put_candles(original_rows)
    config = {'schema_version': 1, 'source': 'fixture', 'asset': 'BTC', 'quote': 'EUR',
              'data_mode': 'demo', 'start_time': 3 * DAY,
              'contributions': [{'time': 3 * DAY, 'amount': '100'}],
              'schedule': [{'time': 3 * DAY, 'amount': '90'}],
              'fees': {'fixed': '1', 'asset': 'quote'}}
    original = engine.issue_assessment('observe-plan', 'BTC', 'fixture', 'EUR', now[0], config)
    return engine, config, original, original_rows, now


def test_plan_outcomes_append_pending_then_90_and_180_days_without_mutating_original(issued):
    engine, config, original, _, now = issued
    identity = original['assessment_id']
    first = engine.observe_assessment(identity, now[0])
    assert first['payload']['plan']['plans']['regular']['terminals']['90']['status'] == 'pending'
    assert first['payload']['plan']['plans']['regular']['terminals']['180']['status'] == 'pending'
    assert engine.observe_assessment(identity, now[0]) == first
    assert len(engine.assessment_outcomes(identity)) == 1

    now[0] = config['start_time'] + 90 * DAY
    engine.store.put_candles(candles(original['issued_at'], now[0], 20))
    day90 = engine.observe_assessment(identity, now[0])
    regular = day90['payload']['plan']['plans']['regular']
    assert regular['terminals']['90']['total_wealth'] == '189'
    assert regular['terminals']['90']['reserve'] == '9'
    assert regular['terminals']['180']['status'] == 'pending'
    assert day90['payload']['plan']['config'] == original['payload']['plan']['config']

    now[0] = config['start_time'] + 180 * DAY
    engine.store.put_candles(candles(config['start_time'] + 90 * DAY, now[0], 30))
    day180 = engine.observe_assessment(identity, now[0])
    regular = day180['payload']['plan']['plans']['regular']
    assert regular['terminals']['180']['total_wealth'] == '279'
    assert regular['terminals']['180']['reserve'] == '9'
    assert day180['payload']['plan']['replay']['pinned_candle_versions'] > 0
    assert engine.assessment(identity) == original
    assert len(engine.assessment_outcomes(identity)) == 3


def test_revised_preissuance_prices_and_retention_cannot_rewrite_original_purchases(issued):
    engine, config, original, original_rows, now = issued
    now[0] = config['start_time'] + 90 * DAY
    engine.store.put_candles(candles(original['issued_at'], now[0], 20))
    engine.store.put_candles([replace(row, open=100, high=100, low=100, close=100,
                                      observed_at=now[0]) for row in original_rows])
    with engine.store.connection() as connection:
        connection.execute('DELETE FROM candles WHERE close_time<=?', (original['issued_at'],))
    observed = engine.observe_assessment(original['assessment_id'], now[0])['payload']['plan']
    assert observed['plans']['regular']['history'] == original['payload']['plan']['plans']['regular']['history']
    assert observed['plans']['regular']['terminals']['90']['total_wealth'] == '189'
    assert observed['replay']['pinned_content_hash']
    assert engine.assessment(original['assessment_id']) == original


def test_missing_terminal_history_stays_pending(issued):
    engine, config, original, _, now = issued
    now[0] = config['start_time'] + 180 * DAY
    observed = engine.observe_assessment(original['assessment_id'], now[0])['payload']['plan']
    assert observed['plans']['regular']['terminals']['90']['status'] == 'pending'
    assert observed['plans']['regular']['terminals']['180']['status'] == 'pending'


def test_issuance_straddling_bar_is_excluded_from_followup_execution(issued):
    engine, config, _, _, now = issued
    now[0] = 4 * DAY + HOUR // 2
    original = engine.issue_assessment('straddled-plan', 'BTC', 'fixture', 'EUR', 4 * DAY, config)
    now[0] = config['start_time'] + 90 * DAY
    future = candles(4 * DAY, now[0], 20)
    future[0] = replace(future[0], open=1, high=1, low=1, close=1)
    engine.store.put_candles(future)
    result = engine.observe_assessment(original['assessment_id'], now[0])['payload']['plan']
    assert result['replay']['excluded_straddling_versions'] == 1
    assert all(purchase['time'] != 4 * DAY for plan in result['plans'].values()
               for purchase in plan['history'])


def test_later_future_price_revision_does_not_rewrite_prior_observed_terminal(issued):
    engine, config, original, _, now = issued
    now[0] = config['start_time'] + 90 * DAY
    future = candles(original['issued_at'], now[0], 20)
    engine.store.put_candles(future)
    first = engine.observe_assessment(original['assessment_id'], now[0])
    now[0] += HOUR
    engine.store.put_candles([replace(future[-1], open=100, high=100, low=100, close=100,
                                      observed_at=now[0])])
    second = engine.observe_assessment(original['assessment_id'], now[0])
    assert first['payload']['plan']['plans']['regular']['terminals']['90'] == second['payload']['plan']['plans']['regular']['terminals']['90']
