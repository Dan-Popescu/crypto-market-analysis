from copy import deepcopy

import pytest

from crypto_market_analysis.derivatives_context import analyze_derivatives_context

NOW = 172800
HOUR = 3600


def observation(time, value, *, metric='open_interest_amount', unit='BTC', **overrides):
    row = {'source': 'synthetic-perpetual', 'asset': 'BTC', 'metric': metric,
           'event_time': time, 'observed_at': time, 'value': value, 'unit': unit,
           'quote': 'USDT', 'data_mode': 'demo', 'metadata': {
               'symbol': 'BTC/USDT:USDT', 'settle': 'USDT', 'market_type': 'swap',
               'counting_convention': 'one_sided'}}
    row.update(overrides)
    return row


def request():
    return {'schema_version': 1, 'asset': 'BTC', 'data_mode': 'demo',
            'observations': [observation(NOW - 24 * HOUR, 90),
                             observation(NOW - 4 * HOUR, 100), observation(NOW, 120)]}


def test_fixed_horizons_describe_native_oi_changes_with_explicit_clocks_and_no_mutation():
    cfg = request()
    original = deepcopy(cfg)
    result = analyze_derivatives_context(cfg, NOW)
    series = result['series'][0]
    assert series['status'] == 'ok'
    assert series['identity']['dimension']['quote'] == 'USDT'
    assert series['identity']['dimension']['counting_convention'] == 'one_sided'
    for label, change, seconds in [('4h', 20, 14400), ('24h', 30, 86400)]:
        comparison = series['comparisons'][label]
        assert comparison['status'] == 'ok'
        assert comparison['native_change'] == change
        assert comparison['change_unit'] == 'BTC'
        assert comparison['start_cutoff'] == NOW - seconds
        assert comparison['elapsed_seconds'] == seconds
        assert comparison['endpoint_skew_seconds'] == 0
        assert comparison['start_point_age_seconds'] == comparison['end_point_age_seconds'] == 0
    assert cfg == original
    assert 'annualized' not in series and 'aggregate' not in result


def test_latest_two_samples_do_not_substitute_for_missing_horizon_endpoint():
    cfg = request()
    cfg['observations'] = [observation(NOW - HOUR, 100), observation(NOW, 120)]
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'ok'
    for comparison in series['comparisons'].values():
        assert comparison['status'] == 'start_missing_endpoint'
        assert comparison['native_change'] is None


def test_later_revision_cannot_rewrite_a_historical_endpoint_and_future_versions_are_excluded():
    cfg = request()
    cutoff = NOW - 4 * HOUR
    cfg['observations'] += [observation(cutoff, 999, observed_at=cutoff + 1),
                            observation(cutoff, 50, observed_at=cutoff - 1),
                            observation(NOW, 777, observed_at=NOW + 1), observation(NOW + 1, 999)]
    result = analyze_derivatives_context(cfg, NOW)
    comparison = result['series'][0]['comparisons']['4h']
    assert comparison['native_change'] == 20
    assert comparison['start']['value'] == 100
    assert comparison['start']['observed_at'] == cutoff
    assert result['excluded']['after_as_of'] == 2


def test_no_backdating_of_a_late_only_historical_event():
    cfg = request()
    cfg['observations'][1]['observed_at'] += 1
    comparison = analyze_derivatives_context(cfg, NOW)['series'][0]['comparisons']['4h']
    assert comparison['status'] == 'start_stale'
    assert comparison['start']['event_time'] == NOW - 24 * HOUR
    assert comparison['native_change'] is None


@pytest.mark.parametrize('field,value', [('source', 'other-native'), ('quote', 'USD'), ('unit', 'ETH')])
def test_different_sources_quotes_and_units_never_form_a_change_or_total(field, value):
    cfg = request()
    cfg['observations'][1][field] = value
    result = analyze_derivatives_context(cfg, NOW)
    assert len(result['series']) == 2
    assert all(series['comparisons']['4h']['native_change'] is None for series in result['series'])
    assert all('total' not in series for series in result['series'])


@pytest.mark.parametrize('key,value', [
    ('symbol', 'BTCUSD-PERP'), ('settle', 'BTC'), ('counting_convention', 'double_sided'),
    ('contract_size', 2), ('contract_size_unit', 'USD'), ('market_type', 'future'), ('expiry', 200000),
])
def test_contract_dimensions_are_preserved_and_never_pooled(key, value):
    cfg = request()
    for row in cfg['observations']:
        row['unit'] = 'contracts'
        row['metadata'].update(contract_size=1, contract_size_unit='BTC')
    cfg['observations'][1]['metadata'][key] = value
    result = analyze_derivatives_context(cfg, NOW)
    assert len(result['series']) == 2
    assert all(series['comparisons']['4h']['native_change'] is None for series in result['series'])


@pytest.mark.parametrize('alias,value', [('funding_interval', '8h'), ('funding_interval_hours', 8),
                                        ('funding_period_hours', 8), ('interval_seconds', 28800)])
def test_funding_aliases_use_public_normalizer_and_keep_native_period(alias, value):
    cfg = request()
    for row, rate in zip(cfg['observations'], [0.0001, 0.0002, 0.0003], strict=True):
        row.update(metric='funding_rate_actual', unit='rate', value=rate)
        row['metadata']['funding_interval_seconds'] = 28800
    del cfg['observations'][1]['metadata']['funding_interval_seconds']
    cfg['observations'][1]['metadata'][alias] = value
    result = analyze_derivatives_context(cfg, NOW)
    assert len(result['series']) == 1
    series = result['series'][0]
    assert series['identity']['dimension']['funding_interval_seconds'] == 28800
    assert series['comparisons']['4h']['native_change'] == pytest.approx(0.0001)
    assert series['comparisons']['24h']['native_change'] == pytest.approx(0.0002)


@pytest.mark.parametrize('metadata', [
    {}, {'funding_interval': '8'}, {'funding_interval_hours': True},
    {'funding_interval_hours': 8, 'interval_seconds': 14400},
    {'funding_interval': '8h', 'funding_interval_seconds': 3600},
])
def test_unknown_or_conflicting_funding_period_stays_unknown(metadata):
    cfg = request()
    for row in cfg['observations']:
        row.update(metric='funding_rate_actual', unit='rate')
        row['metadata'].update(metadata)
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'unknown_native_convention'
    assert all(comparison['native_change'] is None for comparison in series['comparisons'].values())


@pytest.mark.parametrize('change', [
    lambda row: row.update(unit='unknown'), lambda row: row.update(unit=None), lambda row: row.update(quote=None),
    lambda row: row['metadata'].pop('counting_convention'), lambda row: row['metadata'].pop('symbol'),
    lambda row: row['metadata'].pop('settle'), lambda row: row.update(unit='contracts'),
])
def test_unknown_oi_conventions_leave_changes_unknown(change):
    cfg = request()
    for row in cfg['observations']:
        change(row)
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'unknown_native_convention'
    assert series['comparisons']['4h']['native_change'] is None


def test_actual_projected_and_different_funding_periods_remain_independent():
    cfg = request()
    for row in cfg['observations']:
        row.update(metric='funding_rate_actual', unit='rate', value=0.0001)
        row['metadata']['funding_interval'] = '8h'
    cfg['observations'][1]['metric'] = 'funding_rate_projected'
    cfg['observations'][0]['metadata']['funding_interval'] = '4h'
    result = analyze_derivatives_context(cfg, NOW)
    assert len(result['series']) == 3
    assert all(series['comparisons']['4h']['native_change'] is None for series in result['series'])


@pytest.mark.parametrize('metadata', [
    {'value_kind': 'projected'}, {'value_kind': 'actual', 'funding_kind': 'projected'}, {'projected': True},
])
def test_conflicting_actual_projected_declarations_stay_unknown(metadata):
    cfg = request()
    for row in cfg['observations']:
        row.update(metric='funding_rate_actual', unit='rate')
        row['metadata'].update(funding_interval='8h', **metadata)
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'unknown_native_convention'
    assert series['comparisons']['4h']['native_change'] is None


def test_conflicting_oi_currency_metadata_cannot_establish_a_native_change():
    cfg = request()
    for row in cfg['observations']:
        row['metadata']['value_currency'] = 'USD'
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'unknown_native_convention'
    assert series['comparisons']['4h']['native_change'] is None


@pytest.mark.parametrize('value', [None, -1])
def test_unknown_or_negative_oi_value_leaves_the_change_unknown(value):
    cfg = request()
    cfg['observations'][-1]['value'] = value
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'unknown_value'
    assert series['comparisons']['4h']['native_change'] is None


def test_current_stale_status_cannot_be_promoted_by_a_valid_historical_comparison():
    cfg = request()
    cfg['observations'][-1]['event_time'] -= HOUR
    cfg['observations'][-1]['observed_at'] -= HOUR
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'stale'
    assert series['current']['event_age_seconds'] == HOUR
    assert series['comparisons']['4h']['status'] == 'end_stale'
    assert series['comparisons']['4h']['native_change'] is None


@pytest.mark.parametrize('age,expected', [(900, 'ok'), (901, 'endpoint_outside_tolerance')])
def test_explicit_backward_tolerance_and_actual_elapsed_skew_are_reported(age, expected):
    cfg = request()
    cfg['observations'][1]['event_time'] -= age
    cfg['observations'][1]['observed_at'] -= age
    comparison = analyze_derivatives_context(cfg, NOW)['series'][0]['comparisons']['4h']
    assert comparison['status'] == expected
    assert comparison['start_point_age_seconds'] == age
    assert comparison['elapsed_seconds'] == 4 * HOUR + age
    assert comparison['endpoint_skew_seconds'] == age
    assert comparison['native_change'] == (20 if expected == 'ok' else None)


def test_a_point_just_after_historical_target_is_not_accepted_as_nearest_endpoint():
    cfg = request()
    cfg['observations'][1]['event_time'] += 1
    cfg['observations'][1]['observed_at'] += 1
    comparison = analyze_derivatives_context(cfg, NOW)['series'][0]['comparisons']['4h']
    assert comparison['native_change'] is None
    assert comparison['start']['event_time'] == NOW - 24 * HOUR


@pytest.mark.parametrize('field', ['event_time', 'observed_at'])
def test_missing_timestamps_remain_unknown_without_falling_back_to_the_other_clock(field):
    cfg = request()
    for row in cfg['observations']:
        row.pop(field)
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == 'unknown_timestamp'
    assert series['current'] is None
    assert series['unknown_timestamp_observations'] == 3
    assert series['comparisons']['4h']['native_change'] is None


@pytest.mark.parametrize('metadata,status', [
    ({'timestamp_source': 'observation'}, 'unknown_timestamp'),
    ({'historical_known_at': 'unknown'}, 'unknown_availability'),
    ({'status': 'stale'}, 'stale'), ({'status': 'pending'}, 'unknown_observation_status'),
])
def test_fallback_event_times_unknown_availability_and_unavailable_observations_are_not_evidence(metadata, status):
    cfg = request()
    cfg['observations'][-1]['metadata'].update(metadata)
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == status
    assert series['comparisons']['4h']['native_change'] is None


def test_equal_availability_conflicts_fail_closed_and_input_order_is_irrelevant():
    cfg = request()
    cfg['observations'].append(observation(NOW, 999))
    result = analyze_derivatives_context(cfg, NOW)
    cfg['observations'].reverse()
    assert analyze_derivatives_context(cfg, NOW) == result
    assert result['series'][0]['status'] == 'unknown_conflicting_versions'
    assert result['series'][0]['current'] is None
    assert result['series'][0]['comparisons']['4h']['native_change'] is None


def test_exact_duplicates_do_not_conflict_and_later_revision_resolves_a_conflict():
    cfg = request()
    cfg['observations'] += [deepcopy(cfg['observations'][-1])]
    assert analyze_derivatives_context(cfg, NOW)['series'][0]['comparisons']['4h']['native_change'] == 20
    # Conflict at the earlier endpoint is resolved by its last eligible vintage.
    cfg['observations'].extend([observation(NOW - 4 * HOUR, 999, observed_at=NOW - 4 * HOUR - 2),
                                observation(NOW - 4 * HOUR, 888, observed_at=NOW - 4 * HOUR - 2)])
    assert analyze_derivatives_context(cfg, NOW)['series'][0]['comparisons']['4h']['native_change'] == 20


def test_scope_selectors_keep_modes_and_native_quotes_separate_without_fx():
    cfg = request()
    cfg.update(source='synthetic-perpetual', quote='USDT')
    for key, value in [('asset', 'ETH'), ('data_mode', 'live'), ('quote', 'USD'), ('source', 'other')]:
        cfg['observations'].append(observation(NOW, 999, **{key: value}))
    cfg['observations'].append(observation(NOW, 999, metric='spot_volume'))
    result = analyze_derivatives_context(cfg, NOW)
    assert result['excluded'] == {'outside_selection': 4, 'unsupported_metric': 1, 'after_as_of': 0}
    assert len(result['series']) == 1
    assert result['series'][0]['comparisons']['4h']['native_change'] == 20


@pytest.mark.parametrize('change', [
    lambda cfg: cfg.update(schema_version=True), lambda cfg: cfg.update(asset='SOL/USDT'),
    lambda cfg: cfg.pop('data_mode'), lambda cfg: cfg.update(policy={'endpoint_tolerance_seconds': -1}),
    lambda cfg: cfg.update(policy={'endpoint_tolerance_seconds': 3601}),
    lambda cfg: cfg.update(policy={'max_point_age_seconds': True}),
    lambda cfg: cfg['observations'][-1].update(value=float('inf')),
    lambda cfg: cfg['observations'][-1].update(value=10**1000),
    lambda cfg: cfg['observations'][-1].update(observed_at=True),
    lambda cfg: cfg['observations'][-1]['metadata'].update(contract_size=10**1000),
    lambda cfg: cfg['observations'][-1].update(metadata={'bad': float('nan')}),
    lambda cfg: cfg['observations'][-1].update(metadata={'raw': 'x' * 4096}),
    lambda cfg: cfg['observations'][-1].pop('data_mode'),
    lambda cfg: cfg.update(observations=cfg['observations'] * 1667),
])
def test_invalid_requests_and_unbounded_numeric_or_metadata_values_are_rejected(change):
    cfg = request()
    change(cfg)
    with pytest.raises(ValueError):
        analyze_derivatives_context(cfg, NOW)


def test_series_count_is_bounded_and_empty_evidence_is_honestly_empty():
    cfg = request()
    cfg['observations'] = [observation(NOW, 1, source=f'synthetic-{number}') for number in range(65)]
    with pytest.raises(ValueError, match='64'):
        analyze_derivatives_context(cfg, NOW)
    cfg['observations'] = []
    assert analyze_derivatives_context(cfg, NOW)['series'] == []


@pytest.mark.parametrize('native_unit,status', [('SOL', 'ok'), ('UNKNOWN', 'unknown_native_convention'),
                                               ('sol', 'unknown_native_convention'), ('SOL/USDT', 'unknown_native_convention')])
def test_third_asset_contract_size_unit_requires_a_known_canonical_native_code(native_unit, status):
    cfg = request()
    cfg['asset'] = 'SOL'
    for row in cfg['observations']:
        row.update(asset='SOL', unit='contracts')
        row['metadata'].update(symbol='SOL/USDT:USDT', contract_size=0.1, contract_size_unit=native_unit)
    series = analyze_derivatives_context(cfg, NOW)['series'][0]
    assert series['status'] == status
    assert series['comparisons']['4h']['native_change'] == (20 if status == 'ok' else None)
