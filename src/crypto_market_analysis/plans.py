"""Deterministic spot-purchase scenarios; no orders or measured fill claims.

Schema v1 amounts are quote notional, excluding quote fees. Deposits precede
deadline release, activation and fills at equal timestamps. Cash is exclusively allocated at activation;
unfunded amounts are skipped and future deposits never revive them. Targets use
an already completed close as anchor. Fully post-activation bars can simulate a
purchase at their open only; a low touch is evidence, never an executable fill.
Prices include configured spread/slippage and upward price rounding. Quantities
round down. Missing bars postpone execution; targets expire at their wall-clock
deadline. Terminal snapshots include deposits at the cutoff and purchases before
it. All historical execution is a configurable cost assumption.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation, localcontext

from .models import Candle

DAY = 86400
RULE_VERSION = 'accumulation-v1'
MAX_ACTIONS = 2000
ZERO = Decimal(0)
ONE = Decimal(1)


def decimal_string(value: Decimal) -> str:
    return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')


def _decimal(value, name, *, positive=False):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f'{name} must be a bounded decimal string')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be a decimal string') from exc
    if not result.is_finite() or result < 0 or (positive and result == 0) or abs(result.adjusted()) > 30:
        raise ValueError(f'{name} must be finite and nonnegative within supported precision')
    return result


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 10**11:
        raise ValueError(f'{name} must be integer UTC epoch seconds')
    return value


def validate_config(config: dict) -> dict:
    """Return an independent, JSON-safe normalized v1 request."""
    if not isinstance(config, dict) or type(config.get('schema_version')) is not int or config['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    if set(config) - {'schema_version', 'asset', 'quote', 'source', 'data_mode', 'start_time',
                      'contributions', 'schedule', 'starting_holdings', 'fees', 'execution', 'plans'}:
        raise ValueError('Unsupported configuration fields')
    if config.get('asset') not in ('BTC', 'ETH') or config.get('quote') not in ('EUR', 'USD'):
        raise ValueError('Only BTC/ETH and native EUR/USD quotes are supported')
    if not isinstance(config.get('source'), str) or not 1 <= len(config['source']) <= 100:
        raise ValueError('source must be explicit and bounded')
    mode = config.get('data_mode', 'live')
    if mode not in ('live', 'import', 'demo'):
        raise ValueError('Unsupported data mode')
    start = _time(config.get('start_time'), 'start_time')
    result = {key: config[key] for key in ('schema_version', 'asset', 'quote', 'source')}
    result.update(start_time=start, data_mode=mode)
    for name in ('contributions', 'schedule'):
        rows = config.get(name)
        if not isinstance(rows, list) or len(rows) > MAX_ACTIONS:
            raise ValueError(f'{name} must have at most {MAX_ACTIONS} rows')
        result[name] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'time', 'amount'}:
                raise ValueError(f'{name} rows must be objects')
            t = _time(row.get('time'), f'{name}.time')
            if not start <= t <= start + 180 * DAY:
                raise ValueError(f'{name} times must be within the 180-day plan')
            amount = _decimal(row.get('amount'), f'{name}.amount', positive=True)
            result[name].append({'time': t, 'amount': decimal_string(amount)})
        result[name].sort(key=lambda item: item['time'])
    fees = config.get('fees', {})
    if not isinstance(fees, dict) or fees.get('asset', 'quote') not in ('quote', 'base'):
        raise ValueError('Unsupported fee asset; third-asset inventory/conversion is unavailable')
    if set(fees) - {'rate', 'fixed', 'asset'}:
        raise ValueError('Unsupported fee fields')
    result['fees'] = {'asset': fees.get('asset', 'quote')}
    for name in ('fixed', 'rate'):
        result['fees'][name] = decimal_string(_decimal(fees.get(name, '0'), f'fees.{name}'))
    if Decimal(result['fees']['rate']) >= 1:
        raise ValueError('fees.rate must be below 1')
    execution = config.get('execution', {})
    if not isinstance(execution, dict):
        raise ValueError('execution must be an object')
    allowed = {'spread_bps', 'slippage_bps', 'quantity_step', 'price_step', 'assumption',
               'timeframe', 'max_price_age_seconds'}
    if set(execution) - allowed:
        raise ValueError('Unsupported execution fields; aggregated books cannot establish fills')
    result['execution'] = {}
    for name, default in [('spread_bps', '0'), ('slippage_bps', '0'),
                          ('quantity_step', '0.00000001'), ('price_step', '0.00000001')]:
        value = _decimal(execution.get(name, default), f'execution.{name}', positive=name.endswith('step'))
        if name.endswith('bps') and value > 10000:
            raise ValueError('Execution basis points must not exceed 10000')
        result['execution'][name] = decimal_string(value)
    timeframe = execution.get('timeframe', '1h')
    if timeframe not in ('1h', '1d'):
        raise ValueError('Execution timeframe must be 1h or 1d')
    age = _time(execution.get('max_price_age_seconds', 3600), 'max_price_age_seconds')
    if not 0 <= age <= DAY:
        raise ValueError('max_price_age_seconds must be between 0 and 86400')
    assumption = execution.get('assumption', 'historical_configured_costs')
    if assumption != 'historical_configured_costs':
        raise ValueError('Only historical_configured_costs execution is supported')
    result['execution'].update(timeframe=timeframe, max_price_age_seconds=age, assumption=assumption)
    holdings = config.get('starting_holdings', {})
    if not isinstance(holdings, dict):
        raise ValueError('starting_holdings must be an object')
    if set(holdings) - {'quantity', 'cost'}:
        raise ValueError('Unsupported starting holdings fields')
    result['starting_holdings'] = {'quantity': decimal_string(_decimal(holdings.get('quantity', '0'), 'quantity')),
                                   'cost': None}
    if holdings.get('cost') is not None:
        result['starting_holdings']['cost'] = decimal_string(_decimal(holdings['cost'], 'starting cost'))
    plans = config.get('plans', {})
    if not isinstance(plans, dict) or set(plans) - {'split', 'deferred'}:
        raise ValueError('plans supports split and deferred policies')
    split = plans.get('split', {})
    deferred = plans.get('deferred', {})
    if not isinstance(split, dict) or not isinstance(deferred, dict):
        raise ValueError('Policies must be objects')
    if set(split) - {'initial_fraction', 'levels', 'max_wait_days', 'fallback', 'max_deferred_reserve_quote'} or set(deferred) - {'fraction', 'decline', 'max_wait_days', 'fallback', 'max_deferred_reserve_quote'}:
        raise ValueError('Unsupported policy fields')
    result['plans'] = {}
    for name, policy in [('split', split), ('deferred', deferred)]:
        wait = policy.get('max_wait_days', 7)
        if type(wait) is not int or not 1 <= wait <= 90:
            raise ValueError('max_wait_days must be an integer between 1 and 90')
        fallback = policy.get('fallback', 'buy_at_deadline')
        if fallback not in ('buy_at_deadline', 'reserve'):
            raise ValueError('fallback must be buy_at_deadline or reserve')
        cap = policy.get('max_deferred_reserve_quote')
        if cap is not None:
            cap = decimal_string(_decimal(cap, 'max_deferred_reserve_quote'))
        result['plans'][name] = {'max_wait_days': wait, 'fallback': fallback,
                                 'max_deferred_reserve_quote': cap}
    initial = _decimal(split.get('initial_fraction', '0.5'), 'initial_fraction')
    levels = split.get('levels', [{'decline': '0.05', 'fraction': '0.5'}])
    if not isinstance(levels, list) or not 1 <= len(levels) <= 8:
        raise ValueError('split levels must contain 1 to 8 entries')
    normalized = []
    for row in levels:
        if not isinstance(row, dict) or set(row) != {'decline', 'fraction'}:
            raise ValueError('Each split level requires decline and fraction')
        decline = _decimal(row.get('decline'), 'decline', positive=True)
        fraction = _decimal(row.get('fraction'), 'fraction', positive=True)
        if decline >= 1 or fraction > 1:
            raise ValueError('Declines and fractions must be below one')
        normalized.append({'decline': decimal_string(decline), 'fraction': decimal_string(fraction)})
    if initial + sum((Decimal(row['fraction']) for row in normalized), ZERO) != 1:
        raise ValueError('Split fractions must sum to 1')
    result['plans']['split'].update(initial_fraction=decimal_string(initial), levels=normalized)
    fraction = _decimal(deferred.get('fraction', '0.5'), 'deferred.fraction')
    decline = _decimal(deferred.get('decline', '0.05'), 'deferred.decline', positive=True)
    if fraction > 1 or decline >= 1:
        raise ValueError('Deferred fraction or decline is outside supported bounds')
    result['plans']['deferred'].update(fraction=decimal_string(fraction), decline=decimal_string(decline))
    cap_factor = 2 if any(p['max_deferred_reserve_quote'] is not None for p in result['plans'].values()) else 1
    if len(result['schedule']) * (len(normalized) + 1) * cap_factor > MAX_ACTIONS:
        raise ValueError(f'Expanded purchase intents must not exceed {MAX_ACTIONS}')
    return result


def _market_bars(config, candles, as_of, validation):
    if len(candles) > 100000:
        raise ValueError('At most 100000 candle versions are supported')
    latest = {}
    duration = 3600 if config['execution']['timeframe'] == '1h' else DAY
    for candle in candles:
        if (candle.source, candle.asset, candle.quote, candle.data_mode) != (
            config['source'], config['asset'], config['quote'], config['data_mode']
        ):
            raise ValueError('Candle identity must match the explicit reference market')
        if candle.timeframe != config['execution']['timeframe']:
            continue
        for name in ('open_time', 'close_time', 'observed_at'):
            _time(getattr(candle, name), f'candle.{name}')
        if candle.close_time - candle.open_time != duration or candle.open_time % duration:
            raise ValueError('Candles must have complete aligned intervals')
        if candle.close_time > as_of or candle.observed_at > as_of:
            continue
        if validation == 'known_at' and candle.observed_at > candle.close_time + config['execution']['max_price_age_seconds']:
            continue
        values = [Decimal(str(getattr(candle, name))) for name in ('open', 'high', 'low', 'close')]
        if any(not p.is_finite() or p <= 0 for p in values):
            raise ValueError('Candle prices must be positive and finite')
        op, high, low, close = values
        if low > min(op, close) or high < max(op, close) or low > high:
            raise ValueError('Inconsistent candle price range')
        previous = latest.get(candle.open_time)
        if previous is None or (validation == 'retrospective' and candle.observed_at > previous.observed_at) or (validation == 'known_at' and candle.observed_at < previous.observed_at):
            latest[candle.open_time] = candle
        elif candle.observed_at == previous.observed_at and candle != previous:
            raise ValueError('Conflicting candle versions at identical availability')
    return sorted(latest.values(), key=lambda c: c.open_time)


@dataclass
class _Intent:
    created: int
    budget: Decimal
    target: Decimal | None
    deadline: int | None
    fallback: str
    status: str = 'waiting'
    expired: bool = False
    touched: bool = False
    expiry_record: dict | None = None


def _simulate(config, bars, as_of, family, validation):
    fee = config['fees']
    rate, fixed = Decimal(fee['rate']), Decimal(fee['fixed'])
    settings = config['execution']
    qstep, pstep = Decimal(settings['quantity_step']), Decimal(settings['price_step'])
    uplift = ONE + (Decimal(settings['spread_bps']) / 2 + Decimal(settings['slippage_bps'])) / 10000
    free = deposits = spent = fees_quote = fees_base = quantity = ZERO
    starting = Decimal(config['starting_holdings']['quantity'])
    intents, history, expired, touches, funding, unfilled = [], [], [], [], [], []
    terminals, trajectory = {}, []
    units, nav, peak, max_drawdown = ZERO, ONE, ONE, ZERO
    last_mark = None
    close_times = [c.close_time for c in bars]

    def anchor_at(time):
        index = bisect_right(close_times, time) - 1
        while index >= 0:
            candidate = bars[index]
            if validation == 'retrospective' or candidate.observed_at <= time:
                return candidate
            index -= 1
        return None

    by_open = {c.open_time: c for c in bars}
    by_close = {c.close_time: c for c in bars}
    contributions, schedules = {}, {}
    # Schedules end by day 180, waits by day 270, and execution expiry by day 272.
    simulation_end = min(as_of, config['start_time'] + 272 * DAY)
    times = {config['start_time'], simulation_end}
    for row in config['contributions']:
        contributions.setdefault(row['time'], []).append(Decimal(row['amount']))
        times.add(row['time'])
    for row in config['schedule']:
        schedules.setdefault(row['time'], []).append(Decimal(row['amount']))
        times.add(row['time'])
        times.add(row['time'] + settings['max_price_age_seconds'] + 1)
        if family != 'regular':
            deadline = row['time'] + config['plans'][family]['max_wait_days'] * DAY
            times.add(deadline)
            times.add(deadline + settings['max_price_age_seconds'] + 1)
    for c in bars:
        times.update((c.open_time, c.close_time))
    times.update(config['start_time'] + d * DAY for d in (90, 180))

    def reserve():
        return free + sum((i.budget for i in intents if i.status == 'waiting'), ZERO)

    def wealth(price):
        return reserve() + (starting + quantity) * price

    def mark(price):
        nonlocal units, nav, peak, max_drawdown, last_mark
        last_mark = price
        value = wealth(price)
        if units == 0 and value > 0:
            units = value
        if units > 0:
            nav = value / units
            peak = max(peak, nav)
            max_drawdown = max(max_drawdown, ONE - nav / peak)

    def allocate(time, notional, target, deadline, fallback):
        nonlocal free
        if notional <= 0:
            return
        needed = notional * (ONE + rate) + fixed if fee['asset'] == 'quote' else notional
        budget = min(free, needed)
        free -= budget
        funding.append({'time': time, 'requested_notional': decimal_string(notional),
                        'allocated_cash': decimal_string(budget), 'funding_shortfall': budget < needed})
        if budget:
            if target is not None:
                cap = config['plans'][family]['max_deferred_reserve_quote']
                if cap is not None:
                    locked = sum((i.budget for i in intents if i.status == 'waiting' and i.target is not None), ZERO)
                    deferred_budget = min(budget, max(ZERO, Decimal(cap) - locked))
                    if budget > deferred_budget:
                        intents.append(_Intent(time, budget - deferred_budget, None, None, 'buy_at_deadline'))
                    budget = deferred_budget
            if budget:
                intents.append(_Intent(time, budget, target, deadline, fallback))

    for time in sorted(t for t in times if config['start_time'] <= t <= simulation_end):
        bar = by_open.get(time)
        closing = by_close.get(time)
        if closing:
            mark(Decimal(str(closing.close)))
        terminal_boundary = time in (config['start_time'] + 90 * DAY, config['start_time'] + 180 * DAY)
        if bar and not terminal_boundary:
            mark(Decimal(str(bar.open)))
        for amount in contributions.get(time, []):
            # Unit issuance at the immediately preceding portfolio NAV prevents
            # external funding from being mistaken for return or drawdown recovery.
            if units == 0:
                units = amount
                nav = peak = ONE
            elif nav > 0:
                units += amount / nav
            else:
                raise ValueError('Cannot unitize a zero-NAV portfolio')
            free += amount
            deposits += amount
        for days in (90, 180):
            if time == config['start_time'] + days * DAY:
                index = bisect_right(close_times, time) - 1
                valuation = bars[index] if index >= 0 else None
                if valuation and time - valuation.close_time <= settings['max_price_age_seconds']:
                    price = Decimal(str(valuation.close))
                    terminals[str(days)] = {'status': 'complete', 'time': time,
                        'price_time': valuation.close_time, 'price': decimal_string(price),
                        'contributions': decimal_string(deposits), 'holdings_quantity': decimal_string(starting + quantity),
                        'reserve': decimal_string(reserve()), 'total_wealth': decimal_string(wealth(price)),
                        'net_quantity': decimal_string(quantity), 'amount_spent': decimal_string(spent),
                        'fees_quote': decimal_string(fees_quote), 'fees_base': decimal_string(fees_base),
                        'new_purchases_all_in_cost': decimal_string((spent + fees_quote) / quantity) if quantity else None,
                        'maximum_drawdown': decimal_string(max_drawdown)}
                else:
                    terminals[str(days)] = {'status': 'pending', 'time': time, 'reason': 'terminal_price_unavailable'}
        if bar and terminal_boundary:
            mark(Decimal(str(bar.open)))
        # Deadline releases precede new allocations at the same timestamp.
        for intent in intents:
            if intent.status != 'waiting' or intent.deadline is None or time < intent.deadline or intent.expired:
                continue
            intent.expired = True
            intent.expiry_record = {'created': intent.created, 'deadline': intent.deadline,
                                   'target': decimal_string(intent.target), 'fallback': intent.fallback,
                                   'waiting_seconds': intent.deadline - intent.created,
                                   'resolution': 'reserve' if intent.fallback == 'reserve' else 'fallback_pending'}
            expired.append(intent.expiry_record)
            if intent.fallback == 'reserve':
                free += intent.budget
                intent.status = 'expired'
            else:
                intent.target = None
        for amount in schedules.get(time, []):
            anchor_bar = anchor_at(time)
            anchor = Decimal(str(anchor_bar.close)) if anchor_bar and time - anchor_bar.close_time <= settings['max_price_age_seconds'] else None
            if family == 'regular':
                allocate(time, amount, None, None, 'buy_at_deadline')
            else:
                policy = config['plans'][family]
                deadline = time + policy['max_wait_days'] * DAY
                if family == 'split':
                    allocate(time, amount * Decimal(policy['initial_fraction']), None, None, 'buy_at_deadline')
                    levels = policy['levels']
                else:
                    fraction = Decimal(policy['fraction'])
                    allocate(time, amount * (ONE - fraction), None, None, 'buy_at_deadline')
                    levels = [{'fraction': policy['fraction'], 'decline': policy['decline']}]
                for level in levels:
                    if anchor is None:
                        funding.append({'time': time, 'status': 'skipped', 'reason': 'anchor_price_unavailable'})
                    else:
                        allocate(time, amount * Decimal(level['fraction']), anchor * (ONE - Decimal(level['decline'])), deadline, policy['fallback'])
        for intent in intents:
            if intent.status != 'waiting':
                continue
            execution_origin = intent.deadline if intent.expired else intent.created
            if intent.target is None and time > execution_origin + settings['max_price_age_seconds']:
                free += intent.budget
                intent.status = 'expired_unfilled'
                unfilled.append({'created': intent.created, 'time': time, 'status': 'expired_unfilled',
                                 'released_cash': decimal_string(intent.budget),
                                 'reason': 'no_eligible_price_within_execution_window',
                                 'execution_deadline': execution_origin + settings['max_price_age_seconds']})
                if intent.expiry_record is not None:
                    intent.expiry_record['resolution'] = 'expired_unfilled'
                continue
            if not bar or bar.open_time < intent.created:
                continue
            raw_price = Decimal(str(bar.open))
            price = (raw_price * uplift / pstep).to_integral_value(rounding=ROUND_CEILING) * pstep
            if intent.target is not None and price > intent.target:
                if Decimal(str(bar.low)) <= intent.target and not intent.touched:
                    intent.touched = True
                    touches.append({'created': intent.created, 'target': decimal_string(intent.target),
                                    'interval_start': bar.open_time, 'interval_end': bar.close_time,
                                    'status': 'touch_only_not_fill'})
                continue
            available = (intent.budget - fixed) / (ONE + rate) if fee['asset'] == 'quote' else intent.budget
            gross = (max(ZERO, available) / price / qstep).to_integral_value(rounding=ROUND_DOWN) * qstep
            notional = gross * price
            quote_fee = fixed + notional * rate if fee['asset'] == 'quote' and gross else ZERO
            base_fee = fixed + gross * rate if fee['asset'] == 'base' and gross else ZERO
            if gross <= base_fee or gross == 0:
                free += intent.budget
                intent.status = 'dust'
                unfilled.append({'created': intent.created, 'time': time, 'status': 'dust',
                                 'released_cash': decimal_string(intent.budget),
                                 'reason': 'quantity_below_step_or_base_fee'})
                if intent.expiry_record is not None:
                    intent.expiry_record['resolution'] = 'expired_unfilled'
                continue
            free += intent.budget - notional - quote_fee
            intent.status = 'filled'
            if intent.expiry_record is not None:
                intent.expiry_record['resolution'] = 'fallback_filled'
            spent += notional
            fees_quote += quote_fee
            fees_base += base_fee
            quantity += gross - base_fee
            history.append({'time': time, 'created': intent.created, 'price': decimal_string(price),
                            'reference_open': decimal_string(raw_price), 'notional': decimal_string(notional),
                            'gross_quantity': decimal_string(gross), 'net_quantity': decimal_string(gross - base_fee),
                            'fee_quote': decimal_string(quote_fee), 'fee_base': decimal_string(base_fee),
                            'waiting_seconds': time - intent.created, 'deadline_fallback': intent.expired,
                            'execution': 'assumed_full_bar_open'})
        intents = [intent for intent in intents if intent.status == 'waiting']
        if last_mark is not None:
            mark(last_mark)
        if time in contributions or time in schedules or time % DAY == 0 or time == simulation_end or (history and history[-1]['time'] == time):
            trajectory.append({'time': time, 'reserve': decimal_string(reserve()),
                               'quantity': decimal_string(starting + quantity), 'contributions': decimal_string(deposits),
                               'unitized_nav': decimal_string(nav),
                               'total_wealth': decimal_string(wealth(last_mark)) if last_mark is not None else None})
    for days in (90, 180):
        terminals.setdefault(str(days), {'status': 'pending', 'time': config['start_time'] + days * DAY,
                                        'reason': 'future_horizon'})
    total_cost = spent + fees_quote
    known_cost = config['starting_holdings']['cost']
    valuation = bars[-1] if bars and as_of - bars[-1].close_time <= settings['max_price_age_seconds'] else None
    valuation_price = Decimal(str(valuation.close)) if valuation else None
    return {'status': 'simulated' if bars else 'insufficient',
            'total_wealth': decimal_string(wealth(valuation_price)) if valuation_price is not None else None,
            'valuation': {'time': as_of, 'status': 'available' if valuation else 'unavailable',
                          'price_time': valuation.close_time if valuation else None,
                          'price': decimal_string(valuation_price) if valuation_price is not None else None}, 'contributions': decimal_string(deposits),
            'amount_spent': decimal_string(spent), 'fees_quote': decimal_string(fees_quote),
            'fees_base': decimal_string(fees_base), 'reserve': decimal_string(reserve()),
            'unallocated_reserve': decimal_string(free),
            'locked_reserve': decimal_string(reserve() - free), 'net_quantity': decimal_string(quantity),
            'starting_quantity': decimal_string(starting), 'total_quantity': decimal_string(starting + quantity),
            'new_purchases_total_cost': decimal_string(total_cost),
            'new_purchases_all_in_cost': decimal_string(total_cost / quantity) if quantity else None,
            'whole_portfolio_historical_cost': decimal_string(Decimal(known_cost) + total_cost) if known_cost is not None else None,
            'history': history, 'expired_targets': expired, 'touches': touches, 'unfilled_purchases': unfilled,
            'funding': funding, 'terminals': terminals,
            'maximum_drawdown': decimal_string(max_drawdown), 'risk_measurement_end': simulation_end,
            'drawdown_method': 'unitized_NAV_deposits_issue_units_at_pre_deposit_NAV',
            'trajectory': trajectory,
            'pending_targets': sum(i.status == 'waiting' for i in intents),
            'waiting_targets': [{'created': i.created, 'deadline': i.deadline,
                                 'target': decimal_string(i.target) if i.target is not None else None,
                                 'allocated_cash': decimal_string(i.budget),
                                 'waiting_seconds': simulation_end - i.created}
                                for i in intents if i.status == 'waiting']}


def compare_plans(config: dict, candles: list[Candle], as_of: int, *, validation: str = 'retrospective') -> dict:
    """Compare frozen regular/split/deferred rules using one Decimal ledger."""
    as_of = _time(as_of, 'as_of')
    if validation not in ('known_at', 'retrospective'):
        raise ValueError('validation must be known_at or retrospective')
    with localcontext() as ctx:
        ctx.prec = 28
        config = validate_config(config)
        if as_of < config['start_time']:
            raise ValueError('as_of must be at or after start_time')
        bars = _market_bars(config, candles, as_of, validation)
        return {'schema_version': 1, 'rule_version': RULE_VERSION, 'as_of': as_of, 'validation_mode': validation,
                'config': config, 'asset': config['asset'], 'quote': config['quote'],
                'source': config['source'], 'data_mode': config['data_mode'],
                'plans': {family: _simulate(config, bars, as_of, family, validation) for family in ('regular', 'split', 'deferred')},
                'assumptions': ['Historical execution uses configured costs, not measured fills.',
                    'Legacy price floats are converted with Decimal(str(value)); provider precision is not recovered.',
                    'Full post-activation bar opens only; low touches never imply fills; gaps wait for the next eligible bar.',
                    'Reserve is non-interest-bearing native quote cash; no conversion or third-asset fees.',
                    'Purchase amount means quote notional excluding quote fees; cash allocation is exclusive.',
                    'Optional max_deferred_reserve_quote caps locked target cash; overflow buys at the next eligible open.',
                    'Immediate execution expires after creation plus max_price_age_seconds; deadline fallback expires after deadline plus that tolerance.',
                    'Maximum 2000 expanded intents and 100000 candle versions; schedules/contributions span at most 180 days.',
                    ('Strict mode uses earliest timely candle vintages and anchors available at activation; execution remains hypothetical.' if validation == 'known_at' else 'Latest versions available at as_of provide retrospective reference prices; this is not a point-in-time availability claim.'),
                    'Terminals include contributions at the cutoff and purchases strictly before it.'],
                'coverage': {'complete_bars': len(bars), 'start': bars[0].open_time if bars else None,
                             'end': bars[-1].close_time if bars else None}}
