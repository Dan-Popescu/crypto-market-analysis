"""Optional shared dated quote-cash constraints for hypothetical BTC/ETH plans.

This module validates one explicit envelope. It reads no market observations,
portfolio records or account balances, and never converts or nets currencies.
Purchase-plan accounting owns the price-free allocation and release lifecycle.
"""

from decimal import Decimal, localcontext

from .plans import (
    MAX_ACTIONS,
    _decimal,
    _preview_funding,
    _time,
    allocation_specs,
    decimal_string,
    validate_config,
)

MAX_PLANS = 16


def _validate_request(request: dict) -> dict:
    if (not isinstance(request, dict) or type(request.get('schema_version')) is not int
            or request['schema_version'] != 1):
        raise ValueError('schema_version must be 1')
    if set(request) != {'schema_version', 'quote', 'data_mode', 'contributions', 'plans'}:
        raise ValueError('An envelope requires only schema_version, quote, data_mode, contributions and plans')
    if request['quote'] not in ('EUR', 'USD'):
        raise ValueError('An envelope requires one native EUR or USD quote; FX netting is unavailable')
    if request['data_mode'] not in ('live', 'import', 'demo'):
        raise ValueError('An envelope requires one explicit live, import or demo data mode')
    contributions = request['contributions']
    if not isinstance(contributions, list) or len(contributions) > MAX_ACTIONS:
        raise ValueError(f'Envelope contributions must have at most {MAX_ACTIONS} rows')
    normalized = []
    for row in contributions:
        if not isinstance(row, dict) or set(row) != {'time', 'amount'}:
            raise ValueError('Envelope contributions require time and amount')
        normalized.append({'time': _time(row['time'], 'contributions.time'),
                           'amount': decimal_string(_decimal(row['amount'], 'contributions.amount', positive=True))})
    normalized.sort(key=lambda row: row['time'])
    rows = request['plans']
    if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_PLANS:
        raise ValueError(f'An envelope requires 2 to {MAX_PLANS} plans')
    plans, ids, actions = [], set(), 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'family', 'config'}:
            raise ValueError('Each envelope plan requires id, family and config')
        plan_id = row['id']
        if not isinstance(plan_id, str) or not plan_id.strip() or len(plan_id) > 100 or plan_id in ids:
            raise ValueError('Plan ids must be nonempty, bounded and unique')
        ids.add(plan_id)
        family = row['family']
        if family not in ('regular', 'split', 'deferred'):
            raise ValueError('Each plan requires an explicit regular, split or deferred family')
        config = validate_config(row['config'])
        if config['quote'] != request['quote'] or config['data_mode'] != request['data_mode']:
            raise ValueError('Each plan must match the envelope native quote and data mode')
        actions += sum(len(allocation_specs(config, family, Decimal(tranche['amount'])))
                       for tranche in config['schedule'])
        if actions > MAX_ACTIONS:
            raise ValueError(f'Envelope purchase slices must not exceed {MAX_ACTIONS}')
        plans.append({'id': plan_id, 'family': family, 'config': config})
    return {'schema_version': 1, 'quote': request['quote'], 'data_mode': request['data_mode'],
            'contributions': normalized, 'plans': plans}


def preview_budget_envelope(request: dict, as_of: int) -> dict:
    """Validate 2–16 plans against one explicit dated quote-cash envelope.

    Request schema v1 requires ``quote``, ``data_mode``, ``contributions`` (dated
    positive decimal strings) and ``plans`` (``id``, explicit ``family``, existing
    plan ``config``). Only envelope contributions supply shared cash. Config-level
    contributions are validated standalone declarations and are never aggregated.

    At equal timestamps, deposits and every due target release precede tranches.
    Tranches follow request plan order, then each config's stable schedule order;
    slices follow their policy order. Target caps belong to each plan. Future
    deposits cannot revive an earlier shortfall. Immediate and buy-at-deadline
    cash stays committed without inferred fills, refunds or execution expiry.
    """
    as_of = _time(as_of, 'as_of')
    with localcontext() as ctx:
        ctx.prec = 180
        normalized = _validate_request(request)
        preview = _preview_funding(normalized['plans'], normalized['contributions'], as_of)
        return {'schema_version': 1, 'kind': 'shared_price_free_budget_envelope', 'as_of': as_of,
                'quote': normalized['quote'], 'data_mode': normalized['data_mode'],
                'priority': 'declared_plan_order_then_stable_schedule_order_then_policy_slice_order',
                'funding_authority': 'envelope_contributions_only',
                'contributions': normalized['contributions'],
                'future_contributions': [dict(row) for row in normalized['contributions'] if row['time'] > as_of],
                'plans': [{'id': plan['id'], 'family': plan['family'],
                           'market': {key: plan['config'][key] for key in ('asset', 'quote', 'source', 'data_mode')},
                           'fees': plan['config']['fees'],
                           'policy': plan['config']['plans'].get(plan['family']),
                           'standalone_contributions_not_shared_funding': plan['config']['contributions']}
                          for plan in normalized['plans']],
                **preview,
                'assumptions': [
                    'Only envelope contributions fund shared cash; config contributions are standalone declarations, never additional deposits.',
                    'All values are hypothetical quote-cash constraints; actual balances, transactions, holdings and acquisition costs remain unknown here.',
                    'EUR and USD require separate envelopes; native sources may differ, while data modes cannot be mixed.',
                    'Contributions and due reserve releases precede allocations at equal timestamps; declared plan order is the funding priority.',
                    'No future borrowing, fills, rounding refunds or execution-expiry refunds are assumed; earlier shortfalls remain skipped.',
                    'Quote fees consume cash, including a fixed fee per positive policy slice. Base fees reduce unknown received quantities.',
                    'Target caps apply separately to each plan, including quote fees; overflow stays committed as immediate cash.',
                    'Cap overflow can create an additional purchase and fixed fee: allocated cash is unchanged and achievable notional can fall.',
                    'Reserve fallback releases target cash at the deadline; buy-at-deadline fallback retains an immediate commitment.',
                    'Cumulative allocations can include reused released cash; only free plus current commitments equals contributions available.',
                    'Future timeline rows illustrate declared commitments and do not establish future balances or purchases.',
                    'Ledgerly remains authoritative for actual transactions, holdings and acquisition costs.',
                ]}
