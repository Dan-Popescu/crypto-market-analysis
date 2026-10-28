"""Paired held-out scenarios for externally fixed accumulation rules.

No optimization, parameter fitting or predictive-edge claim is performed. Each
origin reuses identical cashflow offsets, costs and terminal dates for all three
policies. Origins are spaced by the longest assessment horizon (180 days).
Strict availability requires a timely origin anchor and timely first candle
vintages; history first downloaded later cannot create a strict origin.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext

from .models import Candle
from .plans import DAY, RULE_VERSION, _market_bars, _time, compare_plans, decimal_string, validate_config

MAX_ORIGINS = 40


def _dispersion(values: list[Decimal]) -> dict:
    if not values:
        return {'mean': None, 'minimum': None, 'maximum': None, 'population_standard_deviation': None}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {'mean': decimal_string(mean), 'minimum': decimal_string(min(values)),
            'maximum': decimal_string(max(values)),
            'population_standard_deviation': decimal_string(variance.sqrt())}


def _difference(left, right, key):
    if left[key] is None or right[key] is None:
        return None
    return decimal_string(Decimal(left[key]) - Decimal(right[key]))


def evaluate_plans(
    config: dict,
    candles: list[Candle],
    as_of: int,
    origins: list[int] | None = None,
    validation: str = 'known_at',
    *,
    held_out_start: int | None = None,
    policy_frozen_at: int | None = None,
) -> dict:
    """Return bounded paired outcomes and their descriptive dispersion.

    ``config.start_time`` is the default declared rule-freeze and held-out start.
    Callers remain responsible for truthfully recording when rules were fixed;
    this function does not turn a retrospective declaration into issued evidence.
    Explicit origins are selected by time alone, never by their outcomes.
    """
    with localcontext() as context:
        context.prec = 28
        return _evaluate(config, candles, as_of, origins, validation, held_out_start, policy_frozen_at)


def _evaluate(config, candles, as_of, origins, validation, held_out_start, policy_frozen_at):
    config = validate_config(config)
    as_of = _time(as_of, 'as_of')
    if validation not in ('known_at', 'retrospective'):
        raise ValueError('validation must be known_at or retrospective')
    frozen = _time(config['start_time'] if policy_frozen_at is None else policy_frozen_at, 'policy_frozen_at')
    held_out = _time(config['start_time'] if held_out_start is None else held_out_start, 'held_out_start')
    if frozen > held_out:
        raise ValueError('Policies must be declared frozen before the held-out period')
    market = _market_bars(config, candles, as_of, validation)
    if origins is None:
        # A deterministic 180-day grid avoids selecting especially good periods.
        origins = list(range(held_out, as_of + 1, 180 * DAY))
    if not isinstance(origins, list) or len(origins) > MAX_ORIGINS:
        raise ValueError(f'origins must contain at most {MAX_ORIGINS} UTC timestamps')
    origins = sorted({_time(origin, 'origin') for origin in origins})
    records, excluded = [], []
    previous = None
    for origin in origins:
        reason = None
        if origin < held_out:
            reason = 'before_held_out_period'
        elif origin > as_of:
            reason = 'future_origin'
        elif previous is not None and origin < previous + 180 * DAY:
            reason = 'overlapping_180_day_origin'
        if reason is not None:
            excluded.append({'origin': origin, 'reason': reason})
            continue
        anchors = [c for c in market if c.close_time <= origin and
                   (validation == 'retrospective' or c.observed_at <= origin)]
        anchor = anchors[-1] if anchors else None
        if anchor is None or origin - anchor.close_time > config['execution']['max_price_age_seconds']:
            excluded.append({'origin': origin, 'reason': 'origin_anchor_unavailable'})
            continue
        shifted = deepcopy(config)
        offset = origin - config['start_time']
        shifted['start_time'] = origin
        for field in ('schedule', 'contributions'):
            for row in shifted[field]:
                row['time'] += offset
        comparison = compare_plans(shifted, candles, as_of, validation=validation)
        plans = comparison['plans']
        outcomes = {}
        for days in ('90', '180'):
            baseline = plans['regular']['terminals'][days]
            if baseline['status'] != 'complete' or any(
                plans[family]['terminals'][days]['status'] != 'complete' for family in ('split', 'deferred')
            ):
                outcomes[days] = {'status': 'pending', 'time': origin + int(days) * DAY}
                continue
            outcomes[days] = {'status': 'complete', 'time': baseline['time'],
                              'valuation_price': baseline['price'], 'price_time': baseline['price_time'],
                              'contributions': baseline['contributions'], 'regular': baseline}
            for family in ('split', 'deferred'):
                terminal = plans[family]['terminals'][days]
                if terminal['contributions'] != baseline['contributions'] or terminal['price'] != baseline['price']:
                    raise ValueError('Paired plans must share cashflows and terminal valuation')
                purchases = [p for p in plans[family]['history'] if p['time'] < terminal['time']]
                diff = _difference(terminal, baseline, 'total_wealth')
                outcomes[days][family] = {
                    'net_quantity': terminal['net_quantity'],
                    'new_purchases_all_in_cost': terminal['new_purchases_all_in_cost'],
                    'total_wealth': terminal['total_wealth'], 'uninvested_reserve': terminal['reserve'],
                    'quantity_difference': _difference(terminal, baseline, 'net_quantity'),
                    'acquisition_cost_difference': _difference(terminal, baseline, 'new_purchases_all_in_cost'),
                    'terminal_wealth_difference': diff,
                    'reserve_difference': _difference(terminal, baseline, 'reserve'),
                    'underperformed_regular': Decimal(diff) < 0,
                    'missed_rising_market_exposure': Decimal(diff) < 0 and Decimal(terminal['price']) > Decimal(str(anchor.close)),
                    'maximum_drawdown': terminal['maximum_drawdown'],
                    'maximum_waiting_seconds': max((p['waiting_seconds'] for p in purchases), default=0),
                    'waiting_seconds_total': sum(p['waiting_seconds'] for p in purchases),
                    'expired_targets': sum(p['deadline'] <= terminal['time'] for p in plans[family]['expired_targets']),
                }
        records.append({'origin': origin, 'anchor_time': anchor.close_time,
                        'anchor_price': decimal_string(Decimal(str(anchor.close))),
                        'outcomes': outcomes})
        previous = origin
    metrics = {}
    for days in ('90', '180'):
        metrics[days] = {}
        for family in ('split', 'deferred'):
            rows = [record['outcomes'][days][family] for record in records
                    if record['outcomes'][days]['status'] == 'complete']
            metrics[days][family] = {'sample_size': len(rows),
                'underperformance_count': sum(row['underperformed_regular'] for row in rows),
                'underperformance_fraction': decimal_string(Decimal(sum(row['underperformed_regular'] for row in rows)) / len(rows)) if rows else None,
                'missed_rising_market_exposure_count': sum(row['missed_rising_market_exposure'] for row in rows),
                'maximum_waiting_seconds': max((row['maximum_waiting_seconds'] for row in rows), default=None)}
            for name in ('quantity_difference', 'acquisition_cost_difference', 'terminal_wealth_difference',
                         'uninvested_reserve', 'reserve_difference', 'maximum_drawdown'):
                metrics[days][family][name] = _dispersion([Decimal(row[name]) for row in rows if row[name] is not None])
    sample_size = sum(any(record['outcomes'][days]['status'] == 'complete' for days in ('90', '180'))
                      for record in records)
    return {'schema_version': 1, 'rule_version': RULE_VERSION,
            'status': 'experimental' if sample_size >= 2 else 'insufficient',
            'as_of': as_of, 'validation_mode': validation,
            'historical_availability_verified': validation == 'known_at',
            'asset': config['asset'], 'quote': config['quote'], 'source': config['source'],
            'data_mode': config['data_mode'], 'config': config, 'sample_size': sample_size,
            'origins': records, 'excluded_origins': excluded, 'metrics': metrics,
            'protocol': {'policy_selection': 'externally_fixed_no_fitting_or_optimization',
                         'policy_frozen_at': frozen, 'held_out_start': held_out,
                         'requested_origins': origins, 'spacing_days': 180,
                         'cashflows': 'identical_amounts_and_origin_relative_offsets',
                         'horizons_days': [90, 180], 'dispersion': 'population_standard_deviation',
                         'drawdown_method': 'unitized_NAV_deposits_issue_units_at_pre_deposit_NAV',
                         'strict_vintages': 'earliest_observation_by_close_plus_configured_max_price_age'},
            'limitations': ['Synthetic correctness and retrospective outcomes do not establish investment performance or predictive edge.',
                'Rule freeze is a caller declaration, not independently verified issuance evidence.',
                'Non-overlapping origins may still be dependent; dispersion is descriptive, not a confidence interval.',
                'Execution uses configured historical costs and conservative full-bar opens; no measured fill curve is available.',
                'Missing bars postpone execution; mark-to-market drawdown omits unobserved intrabar price paths.',
                'Strict evaluation excludes backfills and uses timely first vintages; empty coverage remains empty.',
                'Cash reserve has zero assumed yield and remains in the native quote.']}
