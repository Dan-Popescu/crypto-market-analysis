from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_market_analysis.evidence import canonical_json
from crypto_market_analysis.models import Candle
from crypto_market_analysis.plans import DAY
from crypto_market_analysis.robustness import (
    analyze_plan_robustness,
    build_plan_robustness,
    render_plan_robustness_markdown,
    validate_robustness_request,
)
from crypto_market_analysis.storage import Store


def request():
    return {'schema_version': 1, 'config': {
        'schema_version': 1, 'source': 'test', 'asset': 'BTC', 'quote': 'EUR',
        'start_time': 0, 'contributions': [{'time': 0, 'amount': '250'}],
        'schedule': [{'time': 0, 'amount': '100'}],
        'fees': {'rate': '0', 'fixed': '0', 'asset': 'quote'},
        'execution': {'timeframe': '1d', 'max_price_age_seconds': DAY,
                      'price_step': '1', 'quantity_step': '0.1'},
        'plans': {'deferred': {'fraction': '1', 'decline': '0.2',
                               'max_wait_days': 7, 'fallback': 'reserve'}}},
        'origins': [0, 180 * DAY], 'cost_cases': [{'name': 'declared_zero'}]}


def bars():
    return [Candle('test', 'BTC', 'EUR', '1d', day * DAY, (day + 1) * DAY,
                   price, price, price, price, 1, (day + 1) * DAY)
            for day in range(-1, 361)
            for price in [5 if day in (1, 181) else 10]]


def cell(result, *, origin=0, days=180, cost='declared_zero', missing='observed'):
    return next(row for row in result['evidence'] if row['origin'] == origin
                and row['horizon_days'] == days and row['cost_case'] == cost
                and row['missing_data_case'] == missing)


def test_hand_calculated_conservation_and_native_fee_sign_reversal():
    req = request()
    req['cost_cases'].append({'name': 'fixed_60', 'fees': {'fixed': '60'}})
    result = analyze_plan_robustness(bars(), req, 360 * DAY)
    zero, costly = cell(result), cell(result, cost='fixed_60')
    # 100 buys 10 BTC immediately; split buys 5 at 10 and 10 at 5.
    assert zero['terminals']['regular']['total_wealth'] == '250'
    assert zero['terminals']['split']['total_wealth'] == '300'
    assert zero['paired_differences']['split']['total_wealth'] == '50'
    # Two 60 EUR fixed fees versus one consumes the 50 EUR quantity advantage.
    assert costly['terminals']['regular']['total_wealth'] == '190'
    assert costly['terminals']['split']['total_wealth'] == '180'
    assert costly['paired_differences']['split']['total_wealth'] == '-10'
    assert costly['paired_differences']['split']['fees_quote'] == '60'
    assert costly['paired_differences']['split']['fees_base'] == '0'
    for outcome in (zero, costly):
        for terminal in outcome['terminals'].values():
            assert Decimal(terminal['contributions']) == sum(Decimal(terminal[key]) for key in
                                                            ('amount_spent', 'fees_quote', 'reserve'))
            assert Decimal(terminal['total_wealth']) == (Decimal(terminal['holdings_quantity'])
                                                       * Decimal(terminal['price']) + Decimal(terminal['reserve']))
    summary = result['stability']['180']['split']
    assert summary['wealth_direction'] == 'changes_sign'
    assert summary['wealth_difference_sign_counts'] == {'positive': 2, 'zero': 0, 'negative': 2}
    assert summary['distinct_qualified_origins'] == 2
    assert summary['qualified_pairs'] == 4
    assert summary['qualified_difference_ranges']['total_wealth'] == {
        'minimum': '-10', 'maximum': '50', 'available_pairs': 4}


def test_explicit_missing_hours_remove_all_versions_and_suppress_conclusions():
    req = request()
    req['missing_data_cases'] = [{'name': 'missing_activation', 'removed_hours': [0, 180 * DAY]}]
    result = analyze_plan_robustness(bars(), req, 360 * DAY)
    stressed = cell(result, missing='missing_activation')
    assert stressed['status'] == 'complete'
    assert not stressed['evidence_qualification']['qualified']
    assert stressed['evidence_qualification']['reasons']
    assert result['scenario_coverage'][1]['removed_versions'] == 2
    summary = result['stability']['180']['split']
    assert summary['qualified_pairs'] == 2
    assert summary['excluded_pairs'] == 2
    assert summary['status'] == 'not_enough_evidence'
    assert summary['wealth_direction'] is None
    assert result['status'] == 'not_enough_evidence'


def test_base_fees_keep_native_units_and_reduce_inventory():
    req = request()
    req['config']['fees'] = {'rate': '0', 'fixed': '0.1', 'asset': 'base'}
    result = analyze_plan_robustness(bars(), req, 360 * DAY)
    outcome = cell(result)
    assert outcome['terminals']['regular']['net_quantity'] == '9.9'
    assert outcome['terminals']['split']['net_quantity'] == '14.8'
    assert outcome['paired_differences']['split']['fees_quote'] == '0'
    assert outcome['paired_differences']['split']['fees_base'] == '0.1'
    assert outcome['paired_differences']['split']['total_wealth'] == '49'
    assert outcome['paired_differences']['split']['reserve'] == '0'


def test_late_backfills_and_revisions_do_not_create_known_at_evidence():
    req = request()
    data = bars()
    late = [replace(row, observed_at=360 * DAY) for row in data]
    strict = analyze_plan_robustness(late, req, 360 * DAY)
    assert strict['stability']['180']['split']['qualified_pairs'] == 0
    req['validation'] = 'retrospective'
    retro = analyze_plan_robustness(late, req, 360 * DAY)
    assert retro['stability']['180']['split']['qualified_pairs'] == 2
    assert retro['historical_availability_verified'] is False
    req['validation'] = 'known_at'
    revisions = [replace(row, open=30, high=30, low=30, close=30,
                         observed_at=360 * DAY) for row in data if row.close_time < 350 * DAY]
    original = analyze_plan_robustness(data, req, 360 * DAY)
    revised = analyze_plan_robustness(data + revisions, req, 360 * DAY)
    assert original['evidence'] == revised['evidence']
    assert original['stability'] == revised['stability']


def test_pending_outcomes_and_single_origin_never_count_as_stability():
    req = request()
    req['origins'] = [0]
    result = analyze_plan_robustness(bars(), req, 100 * DAY)
    assert cell(result, days=90)['evidence_qualification']['qualified']
    assert cell(result)['status'] == 'pending'
    assert result['stability']['90']['split']['status'] == 'not_enough_evidence'
    assert result['stability']['90']['split']['qualified_pairs'] == 1
    assert result['stability']['180']['split']['qualified_pairs'] == 0
    assert result['stability']['180']['split']['wealth_difference_sign_counts'] == {
        'positive': 0, 'zero': 0, 'negative': 0}


def test_held_out_and_nonoverlap_exclusions_are_inspectable():
    req = request()
    req['origins'] = [0, 90 * DAY, 180 * DAY]
    result = analyze_plan_robustness(bars(), req, 360 * DAY)
    excluded = cell(result, origin=90 * DAY)
    assert excluded['evidence_qualification']['reasons'] == ['overlapping_180_day_origin']
    # Removing the first origin's anchor cannot admit a different time window.
    req['missing_data_cases'] = [{'name': 'missing_anchor', 'removed_hours': [-DAY, -3600]}]
    req['config']['execution']['max_price_age_seconds'] = 0
    stressed = analyze_plan_robustness(bars(), req, 360 * DAY)
    assert cell(stressed, missing='missing_anchor')['status'] == 'excluded'
    assert cell(stressed, origin=90 * DAY, missing='missing_anchor')[
        'evidence_qualification']['reasons'] == ['overlapping_180_day_origin']
    req['held_out_start'] = 180 * DAY
    held_out = analyze_plan_robustness(bars(), req, 360 * DAY)
    assert cell(held_out)['evidence_qualification']['reasons'] == ['before_held_out_period']
    req['policy_frozen_at'] = 181 * DAY
    with pytest.raises(ValueError, match='frozen'):
        analyze_plan_robustness(bars(), req, 360 * DAY)


@pytest.mark.parametrize('field,value', [('quote', 'USD'), ('asset', 'ETH'),
                                         ('source', 'other'), ('data_mode', 'demo')])
def test_native_identity_cannot_be_mixed_even_in_removed_versions(field, value):
    req = request()
    req['missing_data_cases'] = [{'name': 'remove', 'removed_hours': [0]}]
    data = bars()
    data.append(replace(data[1], **{field: value}))
    with pytest.raises(ValueError, match='identity'):
        analyze_plan_robustness(data, req, 360 * DAY)


def test_unknown_fees_and_unbounded_or_mutating_requests_are_rejected():
    req = request()
    del req['config']['fees']
    with pytest.raises(ValueError, match='unknown fees'):
        validate_robustness_request(req)
    req = request()
    req['cost_cases'][0]['execution'] = {'timeframe': '1h'}
    with pytest.raises(ValueError, match='override'):
        validate_robustness_request(req)
    req = request()
    req['origins'] *= 3
    with pytest.raises(ValueError, match='one to four'):
        validate_robustness_request(req)
    req = request()
    req['missing_data_cases'] = [{'name': 'gap', 'removed_hours': [1]}]
    with pytest.raises(ValueError, match='aligned'):
        validate_robustness_request(req)
    req = request()
    req['config']['schedule'] *= 500
    req['cost_cases'] += [{'name': 'second'}, {'name': 'third'}]
    req['missing_data_cases'] = [{'name': 'gap', 'removed_hours': [0]},
                                 {'name': 'gap2', 'removed_hours': [DAY]}]
    with pytest.raises(ValueError, match='budget'):
        validate_robustness_request(req)
    req['config']['schedule'] = req['config']['schedule'][:300]
    with pytest.raises(ValueError, match='compute budget'):
        validate_robustness_request(req)


def test_input_row_and_result_byte_budgets_fail_explicitly(monkeypatch):
    from crypto_market_analysis import robustness

    with pytest.raises(ValueError, match='100000'):
        analyze_plan_robustness([bars()[0]] * 100001, request(), 360 * DAY)
    monkeypatch.setattr(robustness, 'MAX_RESULT_BYTES', 100)
    with pytest.raises(ValueError, match='result-byte budget'):
        analyze_plan_robustness(bars(), request(), 360 * DAY)


def test_wrapper_reads_one_snapshot_and_does_not_mutate_inputs(tmp_path, monkeypatch):
    store = Store(tmp_path / 'market.sqlite')
    data = [Candle('test', 'BTC', 'EUR', '1h', hour * 3600, (hour + 1) * 3600,
                   10, 10, 10, 10, 1, (hour + 1) * 3600) for hour in range(-1, 360 * 24)]
    store.put_candles(data)
    req = request()
    req['config']['execution'].update(timeframe='1h', max_price_age_seconds=3600)
    before = deepcopy(req)
    original_capture = Store.capture
    calls = []

    def capture_then_commit(self, *args, **kwargs):
        captured = original_capture(self, *args, **kwargs)
        calls.append(captured['watermark'])
        store.put_candles([replace(data[2], open=2, high=2, low=2, close=2,
                                   observed_at=350 * DAY)])
        return captured

    monkeypatch.setattr(Store, 'capture', capture_then_commit)
    result = build_plan_robustness(store, req, 360 * DAY)
    assert len(calls) == 1
    pure = analyze_plan_robustness(data, req, 360 * DAY)
    assert result['evidence'] == pure['evidence']
    assert result['input_reference']['content_hash'] == pure['input_reference']['content_hash']
    assert result['input_reference']['watermark']['candles'] == len(data)
    assert req == before
    serialized = canonical_json(result)
    store.put_candles([replace(data[3], open=1, high=1, low=1, close=1, observed_at=350 * DAY)])
    assert canonical_json(result) == serialized
    markdown = render_plan_robustness_markdown(result)
    assert 'Qualified / declared pairs' in markdown
    assert 'confidence, causality or predictive edge' in markdown


def test_missing_database_stays_missing(tmp_path):
    path = tmp_path / 'missing.sqlite'
    store = Store(path, initialize=False)
    with pytest.raises(ValueError, match='missing'):
        build_plan_robustness(store, request(), 360 * DAY)
    assert not path.exists()
