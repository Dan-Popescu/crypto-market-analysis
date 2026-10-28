from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_market_analysis.models import Candle
from crypto_market_analysis.plans import compare_plans

DAY = 86400


def bars(days=181, price=10):
    return [Candle('test', 'BTC', 'EUR', '1d', d * DAY, (d + 1) * DAY,
                   price, price, price, price, 1, (d + 1) * DAY) for d in range(-1, days)]


def config(**changes):
    value = {'schema_version': 1, 'source': 'test', 'asset': 'BTC', 'quote': 'EUR',
             'data_mode': 'live', 'start_time': 0,
             'contributions': [{'time': 0, 'amount': '100'}],
             'schedule': [{'time': 0, 'amount': '90'}],
             'execution': {'timeframe': '1d', 'quantity_step': '0.00000001',
                           'max_price_age_seconds': DAY}}
    value.update(changes)
    return value


def test_independent_quote_fee_and_base_fee_hand_calculations():
    quote = compare_plans(config(fees={'fixed': '1', 'asset': 'quote'}), bars(), 181 * DAY)['plans']['regular']
    assert quote['reserve'] == '9'
    assert quote['net_quantity'] == '9'
    assert quote['amount_spent'] == '90'
    assert quote['fees_quote'] == '1'
    assert Decimal(quote['new_purchases_all_in_cost']) == Decimal(91) / Decimal(9)
    base = compare_plans(config(fees={'fixed': '0.1', 'asset': 'base'}), bars(), 181 * DAY)['plans']['regular']
    assert base['reserve'] == '10'
    assert base['net_quantity'] == '8.9'
    assert base['fees_base'] == '0.1'
    assert base['whole_portfolio_historical_cost'] is None


def test_future_deposit_cannot_fund_purchase_and_cash_cannot_be_reused():
    cfg = config(contributions=[{'time': 2 * DAY, 'amount': '100'}])
    result = compare_plans(cfg, bars(), 3 * DAY)
    for plan in result['plans'].values():
        assert plan['net_quantity'] == '0'
        assert plan['reserve'] == '100'
    cfg = config(schedule=[{'time': 0, 'amount': '90'}, {'time': DAY, 'amount': '90'}])
    for plan in compare_plans(cfg, bars(), 181 * DAY)['plans'].values():
        assert Decimal(plan['amount_spent']) <= 100
        assert Decimal(plan['reserve']) >= 0
        assert Decimal(plan['amount_spent']) + Decimal(plan['fees_quote']) + Decimal(plan['reserve']) == 100


def test_never_hit_expires_and_same_terminal_wealth_includes_unknown_cost_holdings():
    cfg = config(starting_holdings={'quantity': '2'}, plans={
        'deferred': {'fraction': '1', 'decline': '0.1', 'max_wait_days': 7, 'fallback': 'reserve'}})
    result = compare_plans(cfg, bars(), 181 * DAY)
    delayed = result['plans']['deferred']
    assert delayed['net_quantity'] == '0'
    assert len(delayed['expired_targets']) == 1
    assert delayed['terminals']['90']['total_wealth'] == '120'
    assert delayed['terminals']['90']['time'] == 90 * DAY
    assert delayed['terminals']['180']['time'] == 180 * DAY
    assert delayed['whole_portfolio_historical_cost'] is None


def test_incomplete_outcomes_are_pending_not_latest_price_substitutes():
    result = compare_plans(config(), bars(10), 180 * DAY)
    assert result['plans']['regular']['terminals']['90']['status'] == 'pending'
    assert result['plans']['regular']['terminals']['180']['status'] == 'pending'


def test_straddling_activation_and_future_low_never_fill_at_earlier_open():
    cfg = config(start_time=DAY // 2, contributions=[{'time': DAY // 2, 'amount': '100'}],
                 schedule=[{'time': DAY // 2, 'amount': '90'}])
    data = bars(4)
    data = [replace(c, open=5, low=1) if c.open_time == 0 else c for c in data]
    result = compare_plans(cfg, data, 4 * DAY)['plans']['regular']
    assert result['history'][0]['time'] == DAY
    assert result['history'][0]['price'] == '10'


def test_low_touch_is_not_fill_and_gap_has_explicit_next_bar_open_policy():
    cfg = config(plans={'deferred': {'fraction': '1', 'decline': '0.2', 'max_wait_days': 3,
                                    'fallback': 'reserve'}})
    data = [replace(c, low=7) if c.open_time == 0 else c for c in bars(5)]
    result = compare_plans(cfg, data, 5 * DAY)['plans']['deferred']
    assert result['history'] == []
    assert result['touches'][0]['interval_start'] == 0
    data = [replace(c, open=7, low=7, close=7) if c.open_time == DAY else c for c in data]
    result = compare_plans(cfg, data, 5 * DAY)['plans']['deferred']
    assert result['history'][0]['time'] == DAY
    assert result['history'][0]['price'] == '7'


@pytest.mark.parametrize('price', [5, 10, 20])
def test_flat_falling_and_rising_market_cash_identity(price):
    data = [replace(c, open=price, high=max(10, price), low=min(10, price), close=price)
            if c.open_time >= DAY else c for c in bars()]
    for plan in compare_plans(config(), data, 181 * DAY)['plans'].values():
        assert Decimal(plan['amount_spent']) + Decimal(plan['fees_quote']) + Decimal(plan['reserve']) == 100
        assert Decimal(plan['net_quantity']) >= 0


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-1', True, 12.0])
def test_invalid_money_rejected(bad):
    with pytest.raises(ValueError):
        compare_plans(config(contributions=[{'time': 0, 'amount': bad}]), bars(), DAY)


def test_third_asset_fees_and_mixed_reference_markets_rejected():
    with pytest.raises(ValueError, match='fee asset'):
        compare_plans(config(fees={'asset': 'BNB'}), bars(), DAY)
    with pytest.raises(ValueError, match='identity'):
        compare_plans(config(), bars() + [replace(bars()[0], quote='USD')], DAY)


def test_rounding_dust_and_quote_fee_do_not_overdraw():
    cfg = config(contributions=[{'time': 0, 'amount': '1'}], fees={'rate': '0.1', 'fixed': '0.01'},
                 execution={'timeframe': '1d', 'quantity_step': '0.1', 'price_step': '0.01',
                            'max_price_age_seconds': DAY})
    result = compare_plans(cfg, bars(price=3), DAY)['plans']['regular']
    assert result['net_quantity'] == '0.3'
    assert result['reserve'] == '0'
    assert result['fees_quote'] == '0.1'
