"""Bounded descriptive robustness of externally declared purchase policies.

One native market, one availability mode and one frozen policy are reused across
the entire declared grid. Outcomes never choose origins, costs or missing bars.
This is a historical scenario calculation, not an estimate of predictive edge.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from copy import deepcopy
from decimal import Decimal, localcontext

from .evidence import canonical_json, content_hash
from .models import Candle
from .plans import (
    DAY,
    RULE_VERSION,
    compare_plans,
    decimal_string,
    plan_market_bars,
    validate_config,
    validate_plan_timestamp,
)
from .sensitivity import validated_cost_cases

MAX_ROWS = 100000
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_OPERATION_UNITS = 216
MAX_ACTION_WORK = 100000
MAX_COMPUTE_WORK = 50000000
FAMILIES = ('regular', 'split', 'deferred')
METRICS = ('total_wealth', 'net_quantity', 'reserve', 'fees_quote', 'fees_base',
           'cash_waiting_quote_days', 'cash_weighted_waiting_seconds',
           'maximum_waiting_seconds', 'maximum_drawdown')


def _name(value, names):
    if (not isinstance(value, str) or not 1 <= len(value) <= 40 or value != value.strip()
            or not value.isprintable() or value in names):
        raise ValueError('Scenario names must be unique printable strings of 1 to 40 characters')
    names.add(value)
    return value


def validate_robustness_request(request: dict) -> dict:
    """Normalize a complete, predeclared finite scenario grid before reading data."""
    fields = {'schema_version', 'config', 'origins', 'cost_cases', 'missing_data_cases',
              'validation', 'held_out_start', 'policy_frozen_at'}
    if (not isinstance(request, dict) or set(request) - fields
            or type(request.get('schema_version')) is not int or request['schema_version'] != 1):
        raise ValueError('Robustness request requires schema_version 1 and supported fields')
    original = request.get('config')
    if (not isinstance(original, dict) or not isinstance(original.get('fees'), dict)
            or not {'asset', 'rate', 'fixed'} <= set(original['fees'])):
        raise ValueError('Explicit fees asset, rate and fixed assumptions are required; unknown fees cannot be scored')
    config = validate_config(original)
    origins = request.get('origins')
    if not isinstance(origins, list) or not 1 <= len(origins) <= 4:
        raise ValueError('origins must contain one to four predeclared UTC timestamps')
    origins = [validate_plan_timestamp(origin, 'origin') for origin in origins]
    if len(set(origins)) != len(origins):
        raise ValueError('origins must be unique')
    origins = sorted(origins)
    validation = request.get('validation', 'known_at')
    if validation not in ('known_at', 'retrospective'):
        raise ValueError('validation must be known_at or retrospective')
    frozen = validate_plan_timestamp(request.get('policy_frozen_at', config['start_time']), 'policy_frozen_at')
    held_out = validate_plan_timestamp(request.get('held_out_start', config['start_time']), 'held_out_start')
    if frozen > held_out:
        raise ValueError('Policies must be declared frozen before the held-out period')
    cost_cases = request.get('cost_cases')
    # Existing sensitivity validation pins all policy/timing/identity parameters.
    validated_cost_cases(config, cost_cases)
    missing = request.get('missing_data_cases', [])
    if not isinstance(missing, list) or len(missing) > 2:
        raise ValueError('missing_data_cases must contain at most two scenarios')
    names, normalized_missing = {'observed'}, []
    for case in missing:
        if not isinstance(case, dict) or set(case) != {'name', 'removed_hours'}:
            raise ValueError('Missing data cases require only name and removed_hours')
        name = _name(case['name'], names)
        hours = case['removed_hours']
        if not isinstance(hours, list) or not 1 <= len(hours) <= 168:
            raise ValueError('removed_hours requires one to 168 aligned UTC hour open times')
        hours = [validate_plan_timestamp(hour, 'removed_hour') for hour in hours]
        if len(set(hours)) != len(hours) or any(hour % 3600 for hour in hours):
            raise ValueError('removed_hours must be unique aligned UTC hour open times')
        normalized_missing.append({'name': name, 'removed_hours': sorted(hours)})
    units = len(origins) * len(cost_cases) * (1 + len(missing)) * 2 * len(FAMILIES)
    cap_factor = 2 if any(policy['max_deferred_reserve_quote'] is not None
                          for policy in config['plans'].values()) else 1
    expanded = len(config['schedule']) * (len(config['plans']['split']['levels']) + 1) * cap_factor
    if units > MAX_OPERATION_UNITS or max(1, expanded) * units > MAX_ACTION_WORK:
        raise ValueError('Declared scenario grid exceeds the bounded operation/action work budget')
    # The existing lifecycle visits at most 272 days (including deadline tails).
    # Bound both full-version scans and worst-case event × intent iteration before
    # opening storage, using the maximum accepted capture rather than actual rows.
    duration = 3600 if config['execution']['timeframe'] == '1h' else DAY
    events = 2 * (272 * DAY // duration + 1) + 4 * len(config['schedule']) + len(config['contributions']) + 4
    comparisons = units // (2 * len(FAMILIES))
    work = comparisons * (MAX_ROWS + len(FAMILIES) * events * max(1, expanded))
    if work > MAX_COMPUTE_WORK:
        raise ValueError('Declared scenario grid exceeds the bounded candle/event/intent compute budget')
    return {'schema_version': 1, 'config': config, 'origins': origins,
            'cost_cases': deepcopy(cost_cases), 'missing_data_cases': normalized_missing,
            'validation': validation, 'held_out_start': held_out, 'policy_frozen_at': frozen}


def _difference(row, regular):
    return {name: decimal_string(Decimal(str(row[name])) - Decimal(str(regular[name])))
            if row.get(name) is not None and regular.get(name) is not None else None
            for name in METRICS}


def _summary(evidence, family, days):
    requested = [row for row in evidence if row['horizon_days'] == days]
    qualified = [row for row in requested if row['evidence_qualification']['qualified']]
    distinct = len({row['origin'] for row in qualified})
    complete_grid = len(qualified) == len(requested)
    sufficient = distinct >= 2 and complete_grid
    values = [row['paired_differences'][family] for row in qualified]
    wealth = [Decimal(row['total_wealth']) for row in values]
    signs = {'positive': sum(value > 0 for value in wealth),
             'zero': sum(value == 0 for value in wealth),
             'negative': sum(value < 0 for value in wealth)}
    direction = ('changes_sign' if signs['positive'] and signs['negative'] else
                 'nonnegative' if signs['positive'] else 'nonpositive' if signs['negative'] else 'zero')
    return {'status': 'descriptive_stability' if sufficient else 'not_enough_evidence',
            'requested_pairs': len(requested), 'qualified_pairs': len(qualified),
            'excluded_pairs': len(requested) - len(qualified), 'distinct_qualified_origins': distinct,
            'complete_declared_grid': complete_grid, 'wealth_difference_sign_counts': signs,
            'wealth_direction': direction if sufficient else None,
            'qualified_difference_ranges': {name: {
                'minimum': decimal_string(min(observed)) if observed else None,
                'maximum': decimal_string(max(observed)) if observed else None,
                'available_pairs': len(observed)}
                for name in METRICS
                for observed in [[Decimal(str(row[name])) for row in values if row[name] is not None]]}}


def analyze_plan_robustness(candles: list[Candle], request: dict, as_of: int) -> dict:
    """Calculate fixed-grid historical comparisons with qualified denominators.

    Missing-data cases remove all versions overlapping explicitly declared UTC
    hours. A daily candle is removed in full if any declared hour overlaps it.
    No fabricated bars or fills replace missing observations.
    """
    normalized = validate_robustness_request(request)
    as_of = validate_plan_timestamp(as_of, 'as_of')
    if not isinstance(candles, (list, tuple)) or len(candles) > MAX_ROWS:
        raise ValueError(f'At most {MAX_ROWS} candle versions are supported')
    with localcontext() as context:
        context.prec = 28
        return _analyze(candles, normalized, as_of)


def _analyze(candles, request, as_of):
    config, validation = request['config'], request['validation']
    # Validate every native identity, even rows removed by stress scenarios.
    plan_market_bars(config, candles, as_of, validation)
    variants = [('observed', candles, [])]
    for case in request['missing_data_cases']:
        hours = case['removed_hours']
        rows = []
        for candle in candles:
            index = bisect_left(hours, candle.close_time) - 1
            if index < 0 or hours[index] + 3600 <= candle.open_time:
                rows.append(candle)
        variants.append((case['name'], rows, hours))
    # Spacing is fixed solely by requested times. A missing anchor in one stress
    # case must never admit a different overlapping origin in that case.
    origin_reasons, previous = {}, None
    for origin in request['origins']:
        if origin < request['held_out_start']:
            origin_reasons[origin] = 'before_held_out_period'
        elif origin > as_of:
            origin_reasons[origin] = 'future_origin'
        elif previous is not None and origin < previous + 180 * DAY:
            origin_reasons[origin] = 'overlapping_180_day_origin'
        else:
            previous = origin
    evidence, scenario_coverage = [], []
    for missing_name, rows, hours in variants:
        market = plan_market_bars(config, rows, as_of, validation)
        scenario_coverage.append({'name': missing_name, 'removed_hours': hours,
                                  'removed_versions': len(candles) - len(rows),
                                  'remaining_eligible_bars': len(market)})
        for cost_name, scenario in validated_cost_cases(config, request['cost_cases']):
            for origin in request['origins']:
                reason = origin_reasons.get(origin)
                anchors = [c for c in market if c.close_time <= origin
                           and (validation == 'retrospective' or c.observed_at <= origin)]
                anchor = anchors[-1] if anchors else None
                if reason is None and (anchor is None or origin - anchor.close_time >
                                       config['execution']['max_price_age_seconds']):
                    reason = 'origin_anchor_unavailable'
                comparison = None
                if reason is None:
                    shifted = deepcopy(scenario)
                    offset = origin - config['start_time']
                    shifted['start_time'] = origin
                    for field in ('schedule', 'contributions'):
                        for row in shifted[field]:
                            row['time'] += offset
                    comparison = compare_plans(shifted, rows, as_of, validation=validation)
                for days in (90, 180):
                    terminals = ({family: comparison['plans'][family]['terminals'][str(days)]
                                  for family in FAMILIES} if comparison else {})
                    complete = bool(terminals) and all(row['status'] == 'complete' for row in terminals.values())
                    reasons = ({reason} if reason else
                               {item for row in terminals.values() for item in row['execution_coverage']['reasons']})
                    if not complete and reason is None:
                        reasons.add('terminal_valuation_pending')
                    if complete and any((row['contributions'], row['price']) !=
                                        (terminals['regular']['contributions'], terminals['regular']['price'])
                                        for row in terminals.values()):
                        raise ValueError('Paired plans must share cashflows and terminal valuation')
                    evidence.append({'missing_data_case': missing_name, 'cost_case': cost_name,
                        'origin': origin, 'horizon_days': days, 'terminal_time': origin + days * DAY,
                        'status': 'excluded' if reason else 'complete' if complete else 'pending',
                        'evidence_qualification': {'qualified': complete and not reasons,
                                                   'reasons': sorted(reasons)},
                        'terminals': {family: {key: row.get(key) for key in (
                            'status', 'time', 'price', 'price_time', 'contributions', 'holdings_quantity',
                            'amount_spent', *METRICS, 'execution_coverage')}
                                      for family, row in terminals.items()},
                        'paired_differences': {family: _difference(terminals[family], terminals['regular'])
                                               for family in ('split', 'deferred')} if complete else {}})
    stability = {str(days): {family: _summary(evidence, family, days)
                            for family in ('split', 'deferred')} for days in (90, 180)}
    sufficient = all(item['status'] == 'descriptive_stability'
                     for horizon in stability.values() for item in horizon.values())
    result = {'schema_version': 1, 'rule_version': RULE_VERSION,
        'status': 'descriptive_stability' if sufficient else 'not_enough_evidence',
        'as_of': as_of, 'validation_mode': validation,
        'historical_availability_verified': validation == 'known_at',
        **{key: config[key] for key in ('asset', 'quote', 'source', 'data_mode')},
        'request': request, 'stability': stability, 'scenario_coverage': scenario_coverage,
        'evidence': evidence,
        'input_reference': {'content_hash': content_hash(candles), 'request_hash': content_hash(request),
                            'captured_versions': len(candles)},
        'protocol': {'selection': 'caller_predeclared_no_fitting_optimization_or_winner',
            'baseline_policy': 'regular', 'difference_direction': 'policy_minus_regular',
            'horizons_days': [90, 180], 'origin_spacing_days': 180,
            'policy_frozen_at': request['policy_frozen_at'], 'held_out_start': request['held_out_start'],
            'summary_eligibility': 'complete_qualified_declared_grid_and_at_least_two_distinct_origins',
            'missing_data_rule': 'remove_all_candle_versions_overlapping_declared_UTC_hours',
            'costs': 'explicit_caller_assumptions_including_zero_fees',
            'bounds': {'max_candle_versions': MAX_ROWS, 'max_origins': 4, 'max_cost_cases': 3,
                       'max_missing_data_cases': 2, 'max_operation_units': MAX_OPERATION_UNITS,
                       'max_action_work': MAX_ACTION_WORK, 'max_compute_work': MAX_COMPUTE_WORK,
                       'max_result_bytes': MAX_RESULT_BYTES}},
        'limitations': [
            'Descriptive historical scenarios do not establish prospective performance, confidence, causality or predictive edge.',
            'Origins and cost/missing-data cases are caller declarations; their preselection and rule freeze are not independently verified.',
            'Scenario cells and 90/180-day outcomes reuse observations and are dependent; pair counts are not independent sample sizes.',
            'Unknown fees are rejected. Pending and incomplete execution evidence are excluded from sign counts and ranges.',
            'Direction conclusions are suppressed if any declared cell is unqualified or fewer than two distinct origins qualify.',
            'Missing bars postpone simulated execution and may invalidate comparisons; absent paths never establish observed fills.',
            'Known-at uses timely first vintages; retrospective uses latest versions available at the cutoff. These modes are never pooled.',
            'Fees retain native quote/base units; reserve earns zero yield; drawdown omits unobserved intrabar paths.']}
    if len(canonical_json(result).encode('utf-8')) > MAX_RESULT_BYTES:
        raise ValueError('Robustness result exceeds the bounded result-byte budget')
    return result


def build_plan_robustness(store, request: dict, as_of: int | None = None) -> dict:
    """Capture versions once in one read-only snapshot, then calculate offline."""
    normalized = validate_robustness_request(request)
    now = validate_plan_timestamp(int(time.time()) if as_of is None else as_of, 'as_of')
    config = normalized['config']
    with store.read_session() as session:
        captured = session.capture(config['asset'], now, config['data_mode'], source=config['source'],
                                   quote=config['quote'], profile='candles',
                                   include_candle_versions=True, candle_limit=MAX_ROWS)
        watermark = dict(session.watermark_at_start)
    result = analyze_plan_robustness(captured['candle_versions'], normalized, now)
    result['input_reference']['watermark'] = watermark
    result['input_reference']['capture_profile'] = captured['capture_profile']
    if len(canonical_json(result).encode('utf-8')) > MAX_RESULT_BYTES:
        raise ValueError('Robustness result exceeds the bounded result-byte budget')
    return result


def render_plan_robustness_markdown(result: dict) -> str:
    """Render a compact English topline while JSON retains bounded evidence."""
    lines = ['# Purchase plan robustness', '',
             f"Status: {result['status']}. Market: {result['asset']}/{result['quote']} "
             f"from {result['source']} ({result['data_mode']}).",
             f"Availability: {result['validation_mode']}. Differences are policy minus regular.", '',
             '| Horizon | Policy | Qualified / declared pairs | Distinct origins | Wealth difference range | Direction |',
             '| --- | --- | --- | --- | --- | --- |']
    for days, families in result['stability'].items():
        for family, summary in families.items():
            span = summary['qualified_difference_ranges']['total_wealth']
            lines.append(f"| {days} days | {family} | {summary['qualified_pairs']} / {summary['requested_pairs']} "
                         f"| {summary['distinct_qualified_origins']} | {span['minimum']} to {span['maximum']} "
                         f"{result['quote']} | {summary['wealth_direction'] or 'not_enough_evidence'} |")
    lines.extend(['', *[f'- {item}' for item in result['limitations']], ''])
    return '\n'.join(lines)
