from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_market_analysis.models import Candle
from crypto_market_analysis.plan_evaluation import evaluate_plans
from crypto_market_analysis.plans import compare_plans

DAY = 86400


def bars(days=550, price=10):
    return [Candle('test', 'BTC', 'EUR', '1d', d * DAY, (d + 1) * DAY,
                   price, price, price, price, 1, (d + 1) * DAY) for d in range(-1, days)]


def config():
    return {'schema_version': 1, 'source': 'test', 'asset': 'BTC', 'quote': 'EUR',
            'start_time': 0, 'contributions': [{'time': 0, 'amount': '100'}],
            'schedule': [{'time': 0, 'amount': '100'}],
            'execution': {'timeframe': '1d', 'max_price_age_seconds': DAY},
            'plans': {'deferred': {'fraction': '1', 'decline': '0.2',
                                    'max_wait_days': 7, 'fallback': 'reserve'}}}


def test_backfilled_history_does_not_manufacture_strict_origins():
    data = [replace(c, observed_at=550 * DAY) for c in bars()]
    strict = evaluate_plans(config(), data, 550 * DAY, origins=[0, 180 * DAY, 360 * DAY])
    assert strict['sample_size'] == 0
    assert strict['status'] == 'insufficient'
    retro = evaluate_plans(config(), data, 550 * DAY, origins=[0, 180 * DAY, 360 * DAY],
                           validation='retrospective')
    assert retro['sample_size'] == 3
    assert retro['historical_availability_verified'] is False


def test_rising_markets_show_reserve_and_underperformance_despite_fixed_cashflows():
    data = [replace(c, open=20, high=20, low=20, close=20) if c.open_time > 0 else c for c in bars()]
    result = evaluate_plans(config(), data, 550 * DAY, origins=[0, 180 * DAY, 360 * DAY])
    assert result['sample_size'] == 3
    metrics = result['metrics']['180']['deferred']
    assert metrics['underperformance_count'] == 1
    assert metrics['uninvested_reserve']['mean'] == '100'
    assert Decimal(metrics['terminal_wealth_difference']['minimum']) < 0
    assert result['origins'][0]['outcomes']['180']['deferred']['terminal_wealth_difference'] == '-100'
    assert result['protocol']['policy_selection'] == 'externally_fixed_no_fitting_or_optimization'


def test_nonoverlap_and_heldout_are_explicit_and_no_successful_origins_selected():
    result = evaluate_plans(config(), bars(), 550 * DAY, origins=[0, 90 * DAY, 180 * DAY, 360 * DAY],
                            held_out_start=180 * DAY, policy_frozen_at=0)
    assert [row['origin'] for row in result['origins']] == [180 * DAY, 360 * DAY]
    assert result['protocol']['held_out_start'] == 180 * DAY
    assert len(result['excluded_origins']) == 2
    overlap = evaluate_plans(config(), bars(), 550 * DAY, origins=[0, 90 * DAY, 180 * DAY])
    assert [row['origin'] for row in overlap['origins']] == [0, 180 * DAY]
    with pytest.raises(ValueError):
        evaluate_plans(config(), bars(), 550 * DAY, policy_frozen_at=DAY, held_out_start=0)


def test_pending_horizon_is_not_scored_and_empty_is_honest():
    result = evaluate_plans(config(), bars(100), 100 * DAY, origins=[0])
    assert result['metrics']['90']['deferred']['sample_size'] == 1
    assert result['metrics']['180']['deferred']['sample_size'] == 0
    assert result['origins'][0]['outcomes']['180']['status'] == 'pending'


def test_later_revisions_cannot_change_strict_policy_decisions():
    original = bars()
    revised = [replace(c, open=100, high=100, low=100, close=100,
                       observed_at=c.close_time + 10 * DAY) for c in original]
    first = evaluate_plans(config(), original, 550 * DAY, origins=[0, 180 * DAY])
    second = evaluate_plans(config(), original + revised, 550 * DAY, origins=[0, 180 * DAY])
    assert first['origins'] == second['origins']
    assert first['metrics'] == second['metrics']


def test_external_deposit_does_not_repair_unitized_drawdown():
    cfg = config()
    cfg['contributions'] = [{'time': 0, 'amount': '100'}, {'time': 2 * DAY, 'amount': '100'}]
    data = [replace(c, open=5, high=5, low=5, close=5) if c.open_time >= DAY else c for c in bars()]
    result = compare_plans(cfg, data, 180 * DAY)['plans']['regular']
    assert result['maximum_drawdown'] == '0.5'
    assert result['trajectory'][2]['unitized_nav'] == '0.5'
    assert result['terminals']['180']['maximum_drawdown'] == '0.5'


def test_cash_only_funding_is_not_a_return():
    cfg = config()
    cfg['schedule'] = []
    cfg['contributions'].append({'time': DAY, 'amount': '100'})
    result = compare_plans(cfg, bars(), 180 * DAY)['plans']['regular']
    assert result['maximum_drawdown'] == '0'
    assert {row['unitized_nav'] for row in result['trajectory']} == {'1'}
