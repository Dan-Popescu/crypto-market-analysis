from copy import deepcopy
from decimal import Decimal

import pytest

from crypto_market_analysis.budget import preview_budget_envelope
from crypto_market_analysis.plans import preview_plan_funding

DAY = 86400


def plan(asset, *, time=0, amount='100', family='regular'):
    return {'id': asset.lower(), 'family': family,
            'config': {'schema_version': 1, 'asset': asset, 'quote': 'EUR',
                       'source': f'native-{asset.lower()}', 'data_mode': 'demo', 'start_time': 0,
                       'contributions': [{'time': 0, 'amount': '100'}],
                       'schedule': [{'time': time, 'amount': amount}]}}


def request():
    return {'schema_version': 1, 'quote': 'EUR', 'data_mode': 'demo',
            'contributions': [{'time': 0, 'amount': '100'}],
            'plans': [plan('BTC'), plan('ETH')]}


def assert_conservation(result):
    for snapshot in [result['current'], result['final'], *result['timeline']]:
        cash, committed = Decimal(snapshot['free_cash']), Decimal(snapshot['committed_cash'])
        assert cash >= 0
        assert cash + committed == Decimal(snapshot['contributions_available'])
        assert committed == Decimal(snapshot['locked_cash']) + Decimal(snapshot['reserved_immediate_cash'])
        assert sum((Decimal(row['committed_cash']) for row in snapshot['per_plan'].values()), Decimal(0)) == committed


def test_two_assets_compete_for_one_envelope_without_double_counting_config_deposits():
    cfg = request()
    original = deepcopy(cfg)
    result = preview_budget_envelope(cfg, 0)
    first, second = result['tranches']
    assert (first['required_cash'], first['allocated_cash'], first['shortfall_cash']) == ('100', '100', '0')
    assert (second['required_cash'], second['allocated_cash'], second['shortfall_cash']) == ('100', '0', '100')
    assert result['current']['contributions_available'] == '100'
    assert result['funding_authority'] == 'envelope_contributions_only'
    assert [p['standalone_contributions_not_shared_funding'] for p in result['plans']] == [
        [{'time': 0, 'amount': '100'}], [{'time': 0, 'amount': '100'}]]
    assert cfg == original
    assert_conservation(result)


def test_same_time_priority_is_declared_order_and_not_asset_or_source_sorting():
    cfg = request()
    cfg['plans'].reverse()
    result = preview_budget_envelope(cfg, 0)
    assert [(row['plan_id'], row['allocated_cash']) for row in result['tranches']] == [('eth', '100'), ('btc', '0')]
    # Independent plans for the same asset remain distinct cash commitments.
    cfg['plans'][1]['config']['asset'] = 'ETH'
    assert preview_budget_envelope(cfg, 0)['current']['per_plan']['btc']['committed_cash'] == '0'


def test_future_deposits_do_not_borrow_or_revive_past_shortfalls():
    cfg = request()
    cfg['contributions'] = [{'time': 0, 'amount': '20'}, {'time': DAY, 'amount': '80'}]
    cfg['plans'][1]['config']['schedule'] = [{'time': DAY, 'amount': '100'}]
    cfg['plans'][0]['config']['schedule'].append({'time': 2 * DAY, 'amount': '50'})
    result = preview_budget_envelope(cfg, 0)
    assert [row['allocated_cash'] for row in result['tranches']] == ['20', '80', '0']
    assert [row['phase'] for row in result['tranches']] == ['current', 'future', 'future']
    assert result['current']['contributions_available'] == '20'
    assert result['final']['contributions_available'] == '100'
    assert result['final']['per_plan']['btc']['cumulative_shortfall_cash'] == '130'
    assert result['future_contributions'] == [{'time': DAY, 'amount': '80'}]
    assert_conservation(result)


@pytest.mark.parametrize('fallback,eth_allocated,locked,immediate', [
    ('reserve', '100', '0', '100'), ('buy_at_deadline', '0', '0', '100')])
def test_all_deadline_releases_precede_same_time_other_plan_activation(fallback, eth_allocated, locked, immediate):
    cfg = request()
    btc, eth = cfg['plans']
    btc['family'] = 'deferred'
    btc['config']['plans'] = {'deferred': {'fraction': '1', 'max_wait_days': 1, 'fallback': fallback}}
    eth['config']['schedule'][0]['time'] = DAY
    # The later-activated plan is first in priority; the other plan's expiry
    # still has to run before its tranche, rather than within per-plan loops.
    cfg['plans'] = [eth, btc]
    result = preview_budget_envelope(cfg, DAY)
    assert result['tranches'][1]['allocated_cash'] == eth_allocated
    assert result['current']['locked_cash'] == locked
    assert result['current']['reserved_immediate_cash'] == immediate
    due = next(row for row in result['timeline'] if row['time'] == DAY)
    assert due['deadline_releases'][0]['plan_id'] == 'btc'
    assert due['deadline_releases'][0]['released_cash'] == ('100' if fallback == 'reserve' else '0')
    assert result['current']['free_cash'] == '0'
    assert_conservation(result)


def test_quote_fees_caps_and_partial_funding_conserve_cash_per_plan():
    cfg = request()
    cfg['contributions'][0]['amount'] = '210'
    for row in cfg['plans']:
        row['family'] = 'deferred'
        row['config']['fees'] = {'rate': '0.1', 'fixed': '5', 'asset': 'quote'}
        row['config']['plans'] = {'deferred': {'fraction': '1', 'max_wait_days': 1,
                                              'fallback': 'reserve', 'max_deferred_reserve_quote': '50'}}
    result = preview_budget_envelope(cfg, 0)
    assert [row['required_cash'] for row in result['tranches']] == ['115', '115']
    assert [row['allocated_cash'] for row in result['tranches']] == ['115', '95']
    allocations = [row['allocations'][0] for row in result['tranches']]
    assert [row['locked_cash'] for row in allocations] == ['50', '50']
    assert [row['cap_overflow_immediate_cash'] for row in allocations] == ['65', '45']
    assert result['current']['locked_cash'] == '100'
    assert result['current']['reserved_immediate_cash'] == '110'
    assert result['final']['free_cash'] == '100'
    assert result['final']['committed_cash'] == '110'
    assert_conservation(result)


def test_fixed_fees_apply_to_each_positive_slice_and_not_zero_slices():
    cfg = request()
    cfg['contributions'][0]['amount'] = '120'
    cfg['plans'][0]['family'] = 'split'
    cfg['plans'][0]['config']['fees'] = {'fixed': '10'}
    result = preview_budget_envelope(cfg, 0)
    assert [row['required_cash'] for row in result['tranches'][0]['allocations']] == ['60', '60']
    assert result['tranches'][0]['required_cash'] == '120'
    assert result['tranches'][1]['allocated_cash'] == '0'
    cfg['plans'][0]['config']['plans'] = {'split': {'initial_fraction': '0',
                                                  'levels': [{'decline': '0.1', 'fraction': '1'}]}}
    assert preview_budget_envelope(cfg, 0)['tranches'][0]['required_cash'] == '110'
    assert_conservation(result)


def test_base_fees_and_declared_holdings_never_create_quote_cash_or_asset_estimates():
    cfg = request()
    cfg['plans'][0]['config']['fees'] = {'asset': 'base', 'fixed': '10', 'rate': '0.5'}
    before = preview_budget_envelope(cfg, 0)
    cfg['plans'][0]['config']['starting_holdings'] = {'quantity': '9999999', 'cost': None}
    after = preview_budget_envelope(cfg, 0)
    assert before == after
    assert after['tranches'][0]['required_cash'] == '100'
    assert 'quantity' not in str(after)
    assert_conservation(after)


@pytest.mark.parametrize('quote', ['EUR', 'USD'])
@pytest.mark.parametrize('mode', ['live', 'import', 'demo'])
def test_separate_native_quote_and_mode_envelopes_allow_independent_sources(quote, mode):
    cfg = request()
    cfg.update(quote=quote, data_mode=mode)
    for row in cfg['plans']:
        row['config'].update(quote=quote, data_mode=mode)
    result = preview_budget_envelope(cfg, -1)
    assert result['quote'] == quote and result['data_mode'] == mode
    assert result['current']['contributions_available'] == '0'
    assert [row['market']['source'] for row in result['plans']] == ['native-btc', 'native-eth']
    assert_conservation(result)


def test_duplicate_same_time_schedule_rows_keep_stable_priority_and_cumulative_state():
    cfg = request()
    cfg['plans'][0]['config']['schedule'] = [{'time': 0, 'amount': '40'}, {'time': 0, 'amount': '80'}]
    result = preview_budget_envelope(cfg, 0)
    assert [row['allocated_cash'] for row in result['tranches']] == ['40', '60', '0']
    assert result['current']['per_plan']['btc']['cumulative_requested_notional'] == '120'
    assert result['current']['per_plan']['btc']['cumulative_shortfall_cash'] == '20'
    assert_conservation(result)


def test_expiry_plus_deposit_and_new_target_reset_the_same_plans_cap():
    cfg = request()
    cfg['contributions'].append({'time': DAY, 'amount': '10'})
    btc = cfg['plans'][0]
    btc['family'] = 'deferred'
    btc['config']['schedule'].append({'time': DAY, 'amount': '110'})
    btc['config']['plans'] = {'deferred': {'fraction': '1', 'max_wait_days': 1, 'fallback': 'reserve',
                                          'max_deferred_reserve_quote': '100'}}
    cfg['plans'][1]['config']['schedule'] = []
    result = preview_budget_envelope(cfg, DAY)
    second = result['tranches'][1]
    assert second['cash_available_before'] == '110'
    assert second['allocated_cash'] == '110'
    assert second['allocations'][0]['locked_cash'] == '100'
    assert second['allocations'][0]['cap_overflow_immediate_cash'] == '10'
    assert result['current']['per_plan']['btc']['cumulative_allocated_cash'] == '210'
    assert result['current']['committed_cash'] == '110'
    assert_conservation(result)


def test_all_families_use_existing_single_plan_funding_policy_without_changes():
    cfg = request()
    cfg['plans'][1]['config']['schedule'] = []
    btc = cfg['plans'][0]
    btc['config'].update(fees={'rate': '0.1', 'fixed': '5'}, plans={
        'deferred': {'fraction': '1', 'fallback': 'reserve', 'max_wait_days': 1,
                     'max_deferred_reserve_quote': '50'}})
    single = preview_plan_funding(btc['config'], 0)
    for family in ('regular', 'split', 'deferred'):
        btc['family'] = family
        shared = preview_budget_envelope(cfg, 0)
        assert shared['tranches'][0]['allocations'] == single['families'][family]['tranches'][0]['allocations']
        for key, value in single['families'][family]['current'].items():
            assert shared['current'][key] == value


@pytest.mark.parametrize('change', [
    lambda cfg: cfg.update(schema_version=True),
    lambda cfg: cfg.update(quote='USDT'),
    lambda cfg: cfg.update(data_mode='mixed'),
    lambda cfg: cfg.update(fx_rate='1'),
    lambda cfg: cfg['plans'][1]['config'].update(quote='USD'),
    lambda cfg: cfg['plans'][1]['config'].update(data_mode='live'),
    lambda cfg: cfg['plans'][1]['config'].update(asset='SOL/USD'),
    lambda cfg: cfg['plans'][1]['config'].update(contributions=[{'time': 0, 'amount': 'NaN'}]),
    lambda cfg: cfg['plans'][1].update(id='btc'),
    lambda cfg: cfg['plans'][1].update(family='best'),
    lambda cfg: cfg['plans'][1].pop('family'),
    lambda cfg: cfg.update(plans=cfg['plans'][:1]),
    lambda cfg: cfg.update(plans=cfg['plans'] * 9),
    lambda cfg: cfg['contributions'][0].update(amount='-1'),
    lambda cfg: cfg['contributions'][0].update(time=True),
    lambda cfg: cfg['contributions'][0].update(currency='USD'),
])
def test_ambiguous_or_unbounded_envelopes_fail_closed(change):
    cfg = request()
    change(cfg)
    with pytest.raises(ValueError):
        preview_budget_envelope(cfg, 0)


def test_plan_and_envelope_action_bounds_are_both_enforced():
    cfg = request()
    cfg['contributions'] *= 2001
    with pytest.raises(ValueError, match='contributions'):
        preview_budget_envelope(cfg, 0)
    cfg = request()
    cfg['plans'].append({**plan('BTC'), 'id': 'another-btc'})
    for row in cfg['plans']:
        row['config']['schedule'] *= 700
    with pytest.raises(ValueError, match='Envelope purchase slices'):
        preview_budget_envelope(cfg, 0)


def test_shared_quote_budget_accepts_explicit_generic_asset_without_inventing_prices():
    cfg = request()
    cfg["plans"][1]["config"]["asset"] = "SOL"
    result = preview_budget_envelope(cfg, 0)
    assert result["plans"][1]["market"]["asset"] == "SOL"
    assert_conservation(result)
