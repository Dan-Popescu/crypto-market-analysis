"""Bounded offline sensitivity for caller-declared historical cost assumptions.

The first case is a reference for arithmetic differences, not a recommended
strategy. Policy parameters, funding, origin, bars and terminal dates are fixed.
No fitting, optimization, probabilities or measured execution claims are made.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
from itertools import combinations

from .models import Candle
from .plans import RULE_VERSION, compare_plans, decimal_string, validate_config

FAMILIES = ('regular', 'split', 'deferred')
DIFFERENCE_FIELDS = ('net_quantity', 'amount_spent', 'fees_quote', 'fees_base',
                     'reserve', 'total_wealth', 'new_purchases_all_in_cost',
                     'cash_waiting_quote_days')


def _differences(current, reference):
    return {name: decimal_string(Decimal(current[name]) - Decimal(reference[name]))
            if current.get(name) is not None and reference.get(name) is not None else None
            for name in DIFFERENCE_FIELDS}


def validated_cost_cases(config, cost_cases):
    if not isinstance(cost_cases, list) or not 1 <= len(cost_cases) <= 3:
        raise ValueError('cost_cases must contain one to three named cost cases')
    names = set()
    result = []
    for case in cost_cases:
        if not isinstance(case, dict) or set(case) - {'name', 'fees', 'execution'}:
            raise ValueError('Cost cases support only name, fees and execution overrides')
        name = case.get('name')
        if (not isinstance(name, str) or not 1 <= len(name) <= 40 or name != name.strip()
                or not name.isprintable() or name in names):
            raise ValueError('Cost case names must be unique printable strings of 1 to 40 characters')
        names.add(name)
        scenario = deepcopy(config)
        for field, allowed in [('fees', {'rate', 'fixed', 'asset'}),
                               ('execution', {'spread_bps', 'slippage_bps'})]:
            overrides = case.get(field, {})
            if not isinstance(overrides, dict) or set(overrides) - allowed:
                raise ValueError(f'Unsupported {field} cost override')
            scenario[field].update(overrides)
        result.append((name, validate_config(scenario)))
    return result


def compare_cost_sensitivity(
    config: dict,
    candles: list[Candle],
    as_of: int,
    cost_cases: list[dict],
    validation: str = 'retrospective',
) -> dict:
    """Compare 1–3 named fee/spread/slippage cases on one frozen plan.

    Cases are ``{'name': ..., 'fees': {...}, 'execution': {...}}``. Fee overrides
    accept the existing rate/fixed/asset convention. Execution overrides accept
    only spread_bps/slippage_bps; price/quantity steps and timing stay frozen.
    Signed paired differences are family minus regular. Cross-case differences
    are case minus the first named case. Missing terminal metrics remain null.
    """
    with localcontext() as context:
        context.prec = 28
        normalized = validate_config(config)
        scenarios = validated_cost_cases(normalized, cost_cases)
        cases, comparisons = [], []
        for name, scenario in scenarios:
            comparison = compare_plans(scenario, candles, as_of, validation=validation)
            comparisons.append(comparison)
            plans = comparison['plans']
            outcomes = {}
            for days in ('90', '180'):
                terminals = {family: plans[family]['terminals'][days] for family in FAMILIES}
                complete = all(row['status'] == 'complete' for row in terminals.values())
                reasons = sorted({reason for row in terminals.values()
                                  for reason in row['execution_coverage']['reasons']})
                if not complete:
                    reasons.append('terminal_valuation_pending')
                outcomes[days] = {
                    'status': 'complete' if complete else 'pending',
                    'time': terminals['regular']['time'], **terminals,
                    'paired_differences': {
                        family: _differences(terminals[family], terminals['regular'])
                        for family in ('split', 'deferred')},
                    'evidence_qualification': {'qualified': not reasons, 'reasons': reasons}}
            cases.append({'name': name,
                          'costs': {'fees': scenario['fees'],
                                    'execution': {field: scenario['execution'][field]
                                                  for field in ('spread_bps', 'slippage_bps')}},
                          'plans': plans, 'outcomes': outcomes})
        for case in cases:
            case['differences_from_baseline'] = {
                days: {family: _differences(case['outcomes'][days][family],
                                           cases[0]['outcomes'][days][family])
                       for family in FAMILIES} for days in ('90', '180')}
        reversals = []
        for before, after in combinations(cases, 2):
            for days in ('90', '180'):
                left, right = before['outcomes'][days], after['outcomes'][days]
                if left['status'] != 'complete' or right['status'] != 'complete':
                    continue
                for first, second in combinations(FAMILIES, 2):
                    diffs = [Decimal(row[first]['total_wealth']) - Decimal(row[second]['total_wealth'])
                             for row in (left, right)]
                    if diffs[0] * diffs[1] < 0:
                        reversals.append({'horizon_days': int(days),
                            'between_cases': [before['name'], after['name']],
                            'policies': [first, second],
                            'wealth_differences': [decimal_string(value) for value in diffs],
                            'qualified': left['evidence_qualification']['qualified']
                            and right['evidence_qualification']['qualified']})
        first = comparisons[0]
        return {'schema_version': 1, 'rule_version': RULE_VERSION,
                'status': 'scenario_sensitivity', 'as_of': first['as_of'],
                'validation_mode': validation, 'config': normalized,
                **{field: first[field] for field in ('asset', 'quote', 'source', 'data_mode', 'coverage')},
                'baseline_case': cases[0]['name'], 'cases': cases, 'ranking_reversals': reversals,
                'assumptions': [*first['assumptions'],
                    'All cost cases are caller-declared assumptions; no case is optimized or preferred.',
                    'Cashflows, policy parameters, origin, input bars and 90/180-day terminal dates are fixed across cases.',
                    'Paired differences are policy minus regular; cross-case differences are case minus the first named case.',
                    'Ranking reversals describe hypothetical wealth ordering; qualification requires complete execution paths.',
                    'Quote and base fees retain distinct native units; no implied currency conversion is performed.']}
