from decimal import Decimal

from crypto_market_analysis.models import Candle
from crypto_market_analysis.plan_evaluation import evaluate_plans
from crypto_market_analysis.plans import compare_plans

DAY = 86400


def bars(days=361, price=10):
    return [Candle('test', 'BTC', 'EUR', '1d', d * DAY, (d + 1) * DAY,
                   price, price, price, price, 1, (d + 1) * DAY) for d in range(-1, days)]


def config():
    return {'schema_version': 1, 'source': 'test', 'asset': 'BTC', 'quote': 'EUR',
            'start_time': 0, 'contributions': [{'time': 0, 'amount': '100'}],
            'schedule': [{'time': 0, 'amount': '100'}],
            'execution': {'timeframe': '1d', 'max_price_age_seconds': DAY},
            'plans': {'deferred': {'fraction': '1', 'decline': '0.2',
                                   'max_wait_days': 7, 'fallback': 'reserve'}}}


def test_expired_target_counts_seven_days_of_wait_without_counting_free_cash():
    result = evaluate_plans(config(), bars(), 180 * DAY, origins=[0])
    outcome = result['origins'][0]['outcomes']['180']['deferred']
    assert outcome['maximum_waiting_seconds'] == 7 * DAY
    assert outcome['cash_waiting_quote_days'] == '700'
    assert outcome['target_waiting_quote_days'] == '700'
    assert outcome['execution_waiting_quote_days'] == '0'
    assert outcome['cash_weighted_waiting_seconds'] == str(7 * DAY)
    assert outcome['uninvested_reserve'] == '100'
    assert result['metrics']['180']['deferred']['sample_size'] == 1


def test_pending_wait_is_clipped_at_terminal_and_invariant_to_equivalent_splits():
    cfg = config()
    cfg['schedule'] = [{'time': 89 * DAY, 'amount': '100'}]
    one = compare_plans(cfg, bars(), 180 * DAY)['plans']['deferred']['terminals']['90']
    cfg['schedule'] = [{'time': 89 * DAY, 'amount': '25'}, {'time': 89 * DAY, 'amount': '75'}]
    split = compare_plans(cfg, bars(), 180 * DAY)['plans']['deferred']['terminals']['90']
    for result in (one, split):
        assert result['maximum_waiting_seconds'] == DAY
        assert result['cash_waiting_quote_days'] == '100'
        assert result['cash_weighted_waiting_seconds'] == str(DAY)
        assert result['locked_reserve'] == '100'
    assert one['waiting_seconds_total'] == DAY
    assert split['waiting_seconds_total'] == 2 * DAY


def test_missing_execution_price_counts_unfilled_wait_only_until_cash_release():
    data = [c for c in bars() if c.open_time < 0 or c.open_time >= 7 * DAY]
    result = compare_plans(config(), data, 180 * DAY)['plans']['regular']
    assert result['maximum_waiting_seconds'] == DAY + 1
    assert result['target_waiting_quote_days'] == '0'
    assert Decimal(result['cash_waiting_quote_days']) == Decimal(100 * (DAY + 1)) / DAY
    assert result['terminals']['90']['cash_waiting_quote_days'] == result['cash_waiting_quote_days']


def test_sparse_terminal_prices_preserve_scenarios_but_are_not_performance_samples():
    sparse = [c for c in bars() if c.open_time in {-DAY, 179 * DAY, 359 * DAY}]
    result = evaluate_plans(config(), sparse, 360 * DAY, origins=[0, 180 * DAY])
    assert len(result['origins']) == 2
    assert all(row['outcomes']['180']['status'] == 'complete' for row in result['origins'])
    assert result['sample_size'] == 0
    assert result['scenario_sample_size'] == 2
    assert result['status'] == 'insufficient'
    assert result['metrics']['180']['deferred']['sample_size'] == 0
    assert result['unqualified_scenarios']['180'] == 2
    for row in result['origins']:
        qualification = row['outcomes']['180']['evidence_qualification']
        assert qualification['qualified'] is False
        assert qualification['execution_paths']['regular']['missing_bars'] > 0


def test_intended_reserve_needs_no_execution_bars_after_expiry():
    data = [c for c in bars() if c.open_time <= 7 * DAY or c.open_time == 179 * DAY]
    result = evaluate_plans(config(), data, 180 * DAY, origins=[0])
    outcome = result['origins'][0]['outcomes']['180']
    assert outcome['evidence_qualification']['qualified'] is True
    path = outcome['evidence_qualification']['execution_paths']['deferred']
    assert path['expected_bars'] == 7
    assert path['observed_bars'] == 7
    assert result['sample_size'] == 1
    assert outcome['deferred']['uninvested_reserve'] == '100'


def test_missing_target_path_bar_is_visible_even_if_final_purchase_succeeds():
    cfg = config()
    cfg['plans']['deferred']['fallback'] = 'buy_at_deadline'
    data = [c for c in bars() if c.open_time != 3 * DAY]
    result = evaluate_plans(cfg, data, 180 * DAY, origins=[0])
    outcome = result['origins'][0]['outcomes']['180']
    assert outcome['deferred']['net_quantity'] == '10'
    assert outcome['evidence_qualification']['qualified'] is False
    assert outcome['evidence_qualification']['execution_paths']['deferred']['missing_bars'] == 1
    assert result['metrics']['180']['deferred']['sample_size'] == 0


def test_declared_fixed_fees_reverse_split_discount_with_identical_cashflows():
    from dataclasses import replace

    from crypto_market_analysis.sensitivity import compare_cost_sensitivity

    cfg = config()
    cfg['schedule'] = [{'time': 0, 'amount': '90'}]
    cfg['execution']['quantity_step'] = '0.1'
    cfg['plans']['split'] = {'initial_fraction': '0.5',
                             'levels': [{'decline': '0.1', 'fraction': '0.5'}]}
    data = [replace(c, open=9, low=9, close=9) if c.open_time == DAY else c for c in bars()]
    result = compare_cost_sensitivity(cfg, data, 180 * DAY, [
        {'name': 'low', 'fees': {'fixed': '0'}}, {'name': 'high', 'fees': {'fixed': '6'}}])
    low, high = result['cases']
    for case in (low, high):
        assert case['outcomes']['180']['regular']['contributions'] == '100'
        assert case['outcomes']['180']['split']['contributions'] == '100'
        assert case['outcomes']['180']['evidence_qualification']['qualified'] is True
    assert low['outcomes']['180']['paired_differences']['split']['total_wealth'] == '5'
    assert high['outcomes']['180']['paired_differences']['split']['total_wealth'] == '-1.3'
    assert high['outcomes']['180']['split']['fees_quote'] == '12'
    assert high['outcomes']['180']['split']['reserve'] == '0.7'
    assert high['differences_from_baseline']['180']['split']['total_wealth'] == '-12.3'
    assert any(row['horizon_days'] == 180 and row['policies'] == ['regular', 'split']
               and row['wealth_differences'] == ['-5', '1.3'] for row in result['ranking_reversals'])
    assert 'fixed' not in cfg.get('fees', {})


def test_cost_sensitivity_keeps_pending_horizons_unknown_and_execution_costs_explicit():
    from crypto_market_analysis.sensitivity import compare_cost_sensitivity

    cfg = config()
    cfg['schedule'] = [{'time': 0, 'amount': '90'}]
    cfg['execution']['quantity_step'] = '0.1'
    result = compare_cost_sensitivity(cfg, bars(10), 10 * DAY, [
        {'name': 'declared', 'execution': {'spread_bps': '1000', 'slippage_bps': '500'}}])
    case = result['cases'][0]
    # 10 * (1 + 1000/2/10000 + 500/10000) = 11; 90/11 rounds down to 8.1.
    assert case['plans']['regular']['history'][0]['price'] == '11'
    assert case['plans']['regular']['net_quantity'] == '8.1'
    assert case['plans']['regular']['reserve'] == '10.9'
    assert case['outcomes']['180']['status'] == 'pending'
    assert case['outcomes']['180']['paired_differences']['split']['total_wealth'] is None
    assert result['ranking_reversals'] == []
    assert 'spread_bps' not in cfg['execution']


def test_sensitivity_rejects_policy_search_and_unbounded_or_ambiguous_cases():
    import pytest

    from crypto_market_analysis.sensitivity import compare_cost_sensitivity

    invalid = [[], [{'name': str(i)} for i in range(4)],
               [{'name': 'same'}, {'name': 'same'}],
               [{'name': 'policy', 'plans': {}}],
               [{'name': 'timing', 'execution': {'max_price_age_seconds': 0}}],
               [{'name': 'precision', 'execution': {'quantity_step': '1'}}],
               [{'name': 'nonfinite', 'fees': {'fixed': 'NaN'}}]]
    for cases in invalid:
        with pytest.raises(ValueError):
            compare_cost_sensitivity(config(), bars(), 180 * DAY, cases)
