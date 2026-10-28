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

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation, localcontext

from .models import Candle

DAY = 86400
RULE_VERSION = 'accumulation-v2'
EXECUTION_EVIDENCE_RULE = 'complete_funded_execution_paths_v1'
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


def future_plan_snapshot(config: dict, as_of: int) -> dict:
    """Freeze a validated not-started plan without assuming future observations."""
    config = validate_config(config)
    as_of = _time(as_of, 'as_of')
    if config['start_time'] <= as_of:
        raise ValueError('A future plan must start after its snapshot cutoff')
    return {'schema_version': 1, 'rule_version': RULE_VERSION, 'as_of': as_of,
            'status': 'not_started', 'config': config, 'validation_mode': 'retrospective',
            **{key: config[key] for key in ('source', 'asset', 'quote', 'data_mode')},
            'plans': {family: {'status': 'not_started', 'terminals': {
                str(days): {'status': 'pending', 'time': config['start_time'] + days * DAY,
                            'reason': 'future_horizon'} for days in (90, 180)}}
                      for family in ('regular', 'split', 'deferred')},
            'assumptions': ['The frozen plan has not started; no future price, fill or outcome is assumed.']}


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


def _notional_slices(config: dict, family: str, amount: Decimal) -> list[dict]:
    """Keep zero target slices until the simulator's v2 anchor-evidence check."""
    if family == 'regular':
        return [{'requested_notional': amount, 'decline': None}]
    policy = config['plans'][family]
    initial = (Decimal(policy['initial_fraction']) if family == 'split'
               else ONE - Decimal(policy['fraction']))
    levels = policy['levels'] if family == 'split' else [policy]
    specs = [{'requested_notional': amount * initial, 'decline': None}]
    specs.extend({'requested_notional': amount * Decimal(row['fraction']), 'decline': row['decline']}
                 for row in levels)
    return specs


def allocation_specs(config: dict, family: str, amount: Decimal) -> list[dict]:
    """Describe positive notional slices in the simulator's activation order."""
    specs = _notional_slices(config, family, amount)
    return specs if family == 'regular' else [row for row in specs if row['requested_notional'] > 0]


def required_cash(config: dict, notional: Decimal) -> Decimal:
    """Quote cash for one slice; each positive slice incurs its own fixed fee."""
    fees = config['fees']
    return (notional * (ONE + Decimal(fees['rate'])) + Decimal(fees['fixed'])
            if fees['asset'] == 'quote' and notional > 0 else notional)


@dataclass
class _ActivationFunding:
    """One activation's cash capacity, independent of price-dependent execution.

    Callers supply free cash and existing waiting/target cash after due deposits
    and deadline releases. Only free cash can fund new slices. Future deposits
    are a separate indicative pool, used only by the next-tranche illustration.
    This temporary calculation neither executes nor releases existing intents.
    """

    config: dict
    family: str
    current_cash: Decimal
    target_cash: Decimal = ZERO
    waiting_cash: Decimal = ZERO
    future_cash: Decimal = ZERO

    def allocate(self, time: int, amount: Decimal, *, targets_available: bool = True) -> list[dict]:
        policy = self.config['plans'].get(self.family)
        allocations = []
        for spec in _notional_slices(self.config, self.family, amount):
            decline = spec['decline']
            if decline is not None and not targets_available:
                allocations.append({'time': time, 'status': 'skipped', 'reason': 'anchor_price_unavailable'})
                continue
            if spec['requested_notional'] <= 0:
                continue
            needed = required_cash(self.config, spec['requested_notional'])
            funded_now = min(self.current_cash, needed)
            self.current_cash -= funded_now
            funded_future = min(self.future_cash, needed - funded_now)
            self.future_cash -= funded_future
            allocated = funded_now + funded_future
            target = allocated if decline is not None else ZERO
            if target and policy['max_deferred_reserve_quote'] is not None:
                target = min(target, max(ZERO, Decimal(policy['max_deferred_reserve_quote']) - self.target_cash))
            self.target_cash += target
            self.waiting_cash += allocated
            allocations.append({'requested_notional': decimal_string(spec['requested_notional']),
                'decline': decline, 'required_cash': decimal_string(needed),
                'allocated_cash': decimal_string(allocated), 'funding_shortfall': allocated < needed,
                'funded_from_current_cash': decimal_string(funded_now),
                'funded_from_future_contributions': decimal_string(funded_future),
                'locked_cash': decimal_string(target),
                'cap_overflow_immediate_cash': decimal_string(allocated - target) if decline is not None else '0',
                'deadline': time + policy['max_wait_days'] * DAY if decline is not None else None,
                'fallback': policy['fallback'] if decline is not None else None})
        return allocations


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
    resolved_at: int | None = None


def _waiting_summary(intents, cutoff):
    """Count allocated cash until resolution, excluding subsequent free cash.

    Quote-days and the cash-weighted mean are invariant to splitting an intent
    into identical smaller intents. The legacy unweighted total is descriptive
    only: it depends on the number of intents, rather than economic exposure.
    """
    durations, weighted, target_weighted, allocated = [], ZERO, ZERO, ZERO
    for intent in intents:
        if intent.created >= cutoff:
            continue
        end = min(cutoff, intent.resolved_at if intent.resolved_at is not None else cutoff)
        duration = max(0, end - intent.created)
        durations.append(duration)
        weighted += intent.budget * duration
        allocated += intent.budget
        if intent.deadline is not None:
            target_weighted += intent.budget * max(0, min(end, intent.deadline) - intent.created)
    return {'maximum_waiting_seconds': max(durations, default=0),
            'waiting_seconds_total': sum(durations),
            'cash_waiting_quote_days': decimal_string(weighted / DAY),
            'target_waiting_quote_days': decimal_string(target_weighted / DAY),
            'execution_waiting_quote_days': decimal_string((weighted - target_weighted) / DAY),
            'cash_weighted_waiting_seconds': decimal_string(weighted / allocated) if allocated else None}


def _simulate(config, bars, as_of, family, validation, *, cash_before_activation=False):
    fee = config['fees']
    rate, fixed = Decimal(fee['rate']), Decimal(fee['fixed'])
    settings = config['execution']
    qstep, pstep = Decimal(settings['quantity_step']), Decimal(settings['price_step'])
    uplift = ONE + (Decimal(settings['spread_bps']) / 2 + Decimal(settings['slippage_bps'])) / 10000
    free = deposits = spent = fees_quote = fees_base = quantity = ZERO
    starting = Decimal(config['starting_holdings']['quantity'])
    intents, all_intents, history, expired, touches, funding, unfilled = [], [], [], [], [], [], []
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
    open_times = sorted(by_open)

    def execution_coverage(cutoff):
        # Eligibility depends on declared execution paths, never their returns.
        # A target released into reserve has no further execution requirement.
        duration = 3600 if settings['timeframe'] == '1h' else DAY
        unavailable_windows = 0
        intervals = []
        for intent in all_intents:
            if intent.created >= cutoff:
                continue
            end = cutoff
            if intent.resolved_at is not None and intent.resolved_at < cutoff:
                if intent.status == 'expired_unfilled':
                    execution_origin = intent.deadline if intent.expired else intent.created
                    end = min(end, execution_origin + settings['max_price_age_seconds'] + 1)
                else:
                    end = min(end, intent.resolved_at + (intent.status in ('filled', 'dust')))
            first = -(-intent.created // duration) * duration
            count = max(0, (end - 1 - first) // duration + 1)
            if count:
                intervals.append((first, first + count * duration))
            if not count and intent.status == 'expired_unfilled' and intent.resolved_at < cutoff:
                unavailable_windows += 1
        # Count distinct required intervals, independent of equivalent splitting.
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        expected = sum((end - start) // duration for start, end in merged)
        observed = sum(bisect_left(open_times, end) - bisect_left(open_times, start)
                       for start, end in merged)
        missing_anchors = sum(row.get('reason') == 'anchor_price_unavailable' and row['time'] < cutoff
                              for row in funding)
        missing = expected - observed
        reasons = []
        if missing:
            reasons.append('execution_path_bars_missing')
        if missing_anchors:
            reasons.append('activation_anchor_unavailable')
        if unavailable_windows:
            reasons.append('no_observable_execution_window')
        return {'rule': EXECUTION_EVIDENCE_RULE, 'qualified': not reasons,
                'expected_bars': expected, 'observed_bars': observed,
                'missing_bars': missing, 'missing_activation_anchors': missing_anchors,
                'unobservable_execution_windows': unavailable_windows, 'reasons': reasons}
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
                        'unallocated_reserve': decimal_string(free),
                        'locked_reserve': decimal_string(reserve() - free),
                        'net_quantity': decimal_string(quantity), 'amount_spent': decimal_string(spent),
                        'fees_quote': decimal_string(fees_quote), 'fees_base': decimal_string(fees_base),
                        'new_purchases_all_in_cost': decimal_string((spent + fees_quote) / quantity) if quantity else None,
                        'maximum_drawdown': decimal_string(max_drawdown),
                        'execution_coverage': execution_coverage(time),
                        **_waiting_summary(all_intents, time)}
                else:
                    terminals[str(days)] = {'status': 'pending', 'time': time,
                                           'reason': 'terminal_price_unavailable',
                                           'execution_coverage': execution_coverage(time),
                                           **_waiting_summary(all_intents, time)}
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
                intent.resolved_at = time
            else:
                intent.target = None
        if cash_before_activation and time == as_of:
            return {'free_cash': free,
                    'waiting_cash': sum((i.budget for i in intents if i.status == 'waiting'), ZERO),
                    'target_cash': sum((i.budget for i in intents if i.status == 'waiting' and i.target is not None), ZERO)}
        activation = _ActivationFunding(config, family, free,
            target_cash=sum((i.budget for i in intents if i.status == 'waiting' and i.target is not None), ZERO))
        for amount in schedules.get(time, []):
            anchor_bar = anchor_at(time)
            anchor = Decimal(str(anchor_bar.close)) if anchor_bar and time - anchor_bar.close_time <= settings['max_price_age_seconds'] else None
            for allocation in activation.allocate(time, amount, targets_available=anchor is not None):
                if allocation.get('status') == 'skipped':
                    funding.append(allocation)
                    continue
                funding.append({'time': time, **{key: allocation[key] for key in
                    ('requested_notional', 'allocated_cash', 'funding_shortfall')}})
                budget = Decimal(allocation['allocated_cash'])
                target_budget = Decimal(allocation['locked_cash'])
                if budget > target_budget:
                    intent = _Intent(time, budget - target_budget, None, None, 'buy_at_deadline')
                    intents.append(intent)
                    all_intents.append(intent)
                if target_budget:
                    intent = _Intent(time, target_budget, anchor * (ONE - Decimal(allocation['decline'])),
                                     allocation['deadline'], allocation['fallback'])
                    intents.append(intent)
                    all_intents.append(intent)
        free = activation.current_cash
        for intent in intents:
            if intent.status != 'waiting':
                continue
            execution_origin = intent.deadline if intent.expired else intent.created
            if intent.target is None and time > execution_origin + settings['max_price_age_seconds']:
                free += intent.budget
                intent.status = 'expired_unfilled'
                intent.resolved_at = time
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
                intent.resolved_at = time
                unfilled.append({'created': intent.created, 'time': time, 'status': 'dust',
                                 'released_cash': decimal_string(intent.budget),
                                 'reason': 'quantity_below_step_or_base_fee'})
                if intent.expiry_record is not None:
                    intent.expiry_record['resolution'] = 'expired_unfilled'
                continue
            free += intent.budget - notional - quote_fee
            intent.status = 'filled'
            intent.resolved_at = time
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
                                        'reason': 'future_horizon',
                                        'execution_coverage': execution_coverage(simulation_end),
                                        **_waiting_summary(all_intents, simulation_end)})
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
            **_waiting_summary(all_intents, simulation_end),
            'execution_coverage': execution_coverage(simulation_end),
            'drawdown_method': 'unitized_NAV_deposits_issue_units_at_pre_deposit_NAV',
            'trajectory': trajectory,
            'pending_targets': sum(i.status == 'waiting' for i in intents),
            'waiting_targets': [{'created': i.created, 'deadline': i.deadline,
                                 'target': decimal_string(i.target) if i.target is not None else None,
                                 'allocated_cash': decimal_string(i.budget),
                                 'waiting_seconds': simulation_end - i.created}
                                for i in intents if i.status == 'waiting']}


def next_activation_funding(config: dict, candles: list[Candle], as_of: int, *, current_simulation=None) -> dict | None:
    """Illustrate the next activation using the simulator's event boundary.

    At the current cutoff, due contributions and target expiry are processed
    before cash is inspected, while new allocations and fills are excluded.
    Future activations use current free cash plus declared deposits only: no
    future fills, releases or rounding refunds are assumed.

    current_simulation, when supplied, must be compare_plans output for these
    same inputs and cutoff; the brief reuses its already calculated result.
    """
    config = validate_config(config)
    as_of = _time(as_of, 'as_of')
    upcoming = [row for row in config['schedule'] if row['time'] >= as_of]
    if not upcoming:
        return None
    time = upcoming[0]['time']
    tranches = [row for row in upcoming if row['time'] == time]
    with localcontext() as ctx:
        ctx.prec = 28
        if time == as_of:
            bars = _market_bars(config, candles, as_of, 'retrospective')
            balances = {family: _simulate(config, bars, as_of, family, 'retrospective', cash_before_activation=True)
                        for family in ('regular', 'split', 'deferred')}
        elif as_of >= config['start_time']:
            current = current_simulation or compare_plans(config, candles, as_of)
            balances = {family: {'free_cash': Decimal(row['unallocated_reserve']),
                'waiting_cash': Decimal(row['locked_reserve']),
                'target_cash': sum((Decimal(target['allocated_cash']) for target in row['waiting_targets']
                                    if target['target'] is not None), ZERO)}
                        for family, row in current['plans'].items()}
        else:
            balances = {family: {'free_cash': ZERO, 'waiting_cash': ZERO, 'target_cash': ZERO}
                        for family in ('regular', 'split', 'deferred')}
    with localcontext() as ctx:
        ctx.prec = 180
        future = sum((Decimal(row['amount']) for row in config['contributions']
                      if as_of < row['time'] <= time), ZERO)
        families = {}
        for family, balance in balances.items():
            activation = _ActivationFunding(config, family, balance['free_cash'],
                target_cash=balance['target_cash'], waiting_cash=balance['waiting_cash'], future_cash=future)
            allocations = [allocation for tranche in tranches
                           for allocation in activation.allocate(time, Decimal(tranche['amount']))]
            families[family] = {'current_cash_available': decimal_string(balance['free_cash']),
                'future_contributions_before_activation': decimal_string(future),
                'immediate_notional': decimal_string(sum((Decimal(row['requested_notional']) for row in allocations
                                                         if row['decline'] is None), ZERO)),
                'deferred_notional': decimal_string(sum((Decimal(row['requested_notional']) for row in allocations
                                                        if row['decline'] is not None), ZERO)),
                'allocations': allocations,
                'free_cash_after': decimal_string(activation.current_cash + activation.future_cash),
                'locked_cash_after': decimal_string(activation.waiting_cash),
                'target_locked_cash_before': decimal_string(balance['target_cash']),
                'target_locked_cash_after': decimal_string(activation.target_cash),
                'funding_shortfall': any(row['funding_shortfall'] for row in allocations)}
        return {'time': time,
                'requested_notional': decimal_string(sum((Decimal(row['amount']) for row in tranches), ZERO)),
                'families': families, 'future_contributions_before_activation': decimal_string(future),
                'funding_basis': 'Cash-capacity illustration using hypothetical current free reserve plus deposits due by activation; activation still requires eligible prices. No borrowing, future fills, releases or rounding refunds assumed.',
                'future_anchor_unknown': time > as_of}


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
                    'Waiting covers all funded intents until fill, expiry or cash release, clipped at each cutoff; free cash is excluded.',
                    'Cash quote-days and cash-weighted waiting are invariant to equivalent splits; waiting_seconds_total is an intent-count-dependent legacy total.',
                    'Execution evidence requires every aligned bar over funded active paths; intended reserve after expiry needs no further execution bars.',
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


def policies_equivalent(config: dict) -> bool:
    """Identify split and deferred policies with the same declared allocations."""
    split, deferred = config["plans"]["split"], config["plans"]["deferred"]
    common = ("max_wait_days", "fallback", "max_deferred_reserve_quote")
    return (all(split[key] == deferred[key] for key in common)
            and Decimal(split["initial_fraction"]) == 1 - Decimal(deferred["fraction"])
            and split["levels"] == [{"decline": deferred["decline"], "fraction": deferred["fraction"]}])


def _preview_funding(plans: list[dict], contributions: list[dict], as_of: int) -> dict:
    """Own price-free commitment lifecycle for one plan or a shared cash pool.

    Inputs are normalized; only ``contributions`` fund this pool. Plans and their
    same-time schedule rows retain declared order. Each plan owns its target cap,
    while all plans compete for the same free cash after deposits and releases.
    """
    cash, deposits = ZERO, ZERO
    commitments, tranches, timeline = [], [], []
    events = {as_of}
    deposits_at, schedules = {}, {}
    totals = {plan['id']: {key: ZERO for key in ('requested', 'required', 'allocated', 'shortfall')}
              for plan in plans}
    for row in contributions:
        deposits_at.setdefault(row['time'], []).append(Decimal(row['amount']))
        events.add(row['time'])
    for plan in plans:
        policy = plan['config']['plans'].get(plan['family'])
        for row in plan['config']['schedule']:
            schedules.setdefault(row['time'], []).append((plan, Decimal(row['amount'])))
            events.add(row['time'])
            if policy:
                events.add(row['time'] + policy['max_wait_days'] * DAY)

    def snapshot():
        balances = {plan['id']: {'locked_cash': ZERO, 'reserved_immediate_cash': ZERO} for plan in plans}
        for commitment in commitments:
            key = 'locked_cash' if commitment['locked'] else 'reserved_immediate_cash'
            balances[commitment['plan_id']][key] += commitment['cash']
        per_plan = {}
        for plan_id, balance in balances.items():
            per_plan[plan_id] = {key: decimal_string(value) for key, value in balance.items()}
            per_plan[plan_id]['committed_cash'] = decimal_string(sum(balance.values(), ZERO))
            per_plan[plan_id].update({f'cumulative_{key}_cash' if key != 'requested'
                                     else 'cumulative_requested_notional': decimal_string(value)
                                     for key, value in totals[plan_id].items()})
        locked = sum((balance['locked_cash'] for balance in balances.values()), ZERO)
        immediate = sum((balance['reserved_immediate_cash'] for balance in balances.values()), ZERO)
        return {'contributions_available': decimal_string(deposits), 'free_cash': decimal_string(cash),
                'locked_cash': decimal_string(locked), 'reserved_immediate_cash': decimal_string(immediate),
                'committed_cash': decimal_string(locked + immediate), 'per_plan': per_plan}

    current = None
    for time in sorted(events):
        contributed = sum(deposits_at.get(time, []), ZERO)
        cash += contributed
        deposits += contributed
        releases, activations = [], []
        for commitment in commitments:
            if commitment['locked'] and time >= commitment['deadline']:
                commitment['locked'] = False
                amount = commitment['cash']
                released = amount if commitment['fallback'] == 'reserve' else ZERO
                cash += released
                commitment['cash'] -= released
                releases.append({'plan_id': commitment['plan_id'], 'fallback': commitment['fallback'],
                                 'released_cash': decimal_string(released),
                                 'retained_commitment_cash': decimal_string(amount - released)})
        for plan, amount in schedules.get(time, []):
            plan_id, config, family = plan['id'], plan['config'], plan['family']
            available = cash
            activation = _ActivationFunding(config, family, cash,
                target_cash=sum((c['cash'] for c in commitments if c['locked'] and c['plan_id'] == plan_id), ZERO))
            allocations = activation.allocate(time, amount)
            for allocation in allocations:
                locked = Decimal(allocation['locked_cash'])
                if locked:
                    commitments.append({'plan_id': plan_id, 'cash': locked, 'locked': True,
                                        'deadline': allocation['deadline'], 'fallback': allocation['fallback']})
                immediate = Decimal(allocation['allocated_cash']) - locked
                if immediate:
                    commitments.append({'plan_id': plan_id, 'cash': immediate, 'locked': False,
                                        'deadline': None, 'fallback': None})
            cash = activation.current_cash
            required = sum((Decimal(row['required_cash']) for row in allocations), ZERO)
            allocated = sum((Decimal(row['allocated_cash']) for row in allocations), ZERO)
            for key, value in [('requested', amount), ('required', required),
                               ('allocated', allocated), ('shortfall', required - allocated)]:
                totals[plan_id][key] += value
            row = {'time': time, 'phase': 'future' if time > as_of else 'current' if time == as_of else 'past',
                   'plan_id': plan_id, 'requested_notional': decimal_string(amount),
                   'required_cash': decimal_string(required), 'allocated_cash': decimal_string(allocated),
                   'shortfall_cash': decimal_string(required - allocated),
                   'cash_available_before': decimal_string(available),
                   'allocations': [{key: value for key, value in allocation.items()
                                    if key not in ('funded_from_current_cash', 'funded_from_future_contributions')}
                                   for allocation in allocations],
                   'funding_shortfall': allocated < required, 'free_cash_after': decimal_string(cash)}
            tranches.append(row)
            activations.append(row)
        balance = snapshot()
        if time == as_of:
            current = balance
        timeline.append({'time': time, 'phase': 'future' if time > as_of else 'current' if time == as_of else 'past',
                         'contributed_cash': decimal_string(contributed), 'deadline_releases': releases,
                         'tranches': activations, **balance})
    return {'current': current, 'final': snapshot(), 'timeline': timeline, 'tranches': tranches}


def _preview_family(config: dict, as_of: int, family: str) -> dict:
    """Preserve the single-plan preview contract over the shared lifecycle."""
    preview = _preview_funding([{'id': family, 'config': config, 'family': family}],
                               config['contributions'], as_of)
    current = {key: preview['current'][key] for key in (
        'contributions_available', 'free_cash', 'locked_cash', 'reserved_immediate_cash')}
    rows = [{key: value for key, value in row.items()
             if key not in ('plan_id', 'required_cash', 'allocated_cash', 'shortfall_cash')}
            for row in preview['tranches']]
    return {'current': current, 'tranches': rows,
            'next_tranche': next((row for row in rows if row['time'] >= as_of), None)}


def preview_plan_funding(config: dict, as_of: int) -> dict:
    """Preview declared cash constraints without market prices or actual holdings.

    Deposits become available at their timestamps. Cash assigned to an immediate
    purchase remains committed; target cash releases only on reserve fallback.
    Thus this preview cannot claim execution, remaining account cash or rounding
    refunds. The historical simulator supplies those separate hypothetical paths.
    """
    config = validate_config(config)
    as_of = _time(as_of, "as_of")
    with localcontext() as ctx:
        ctx.prec = 180
        return {"schema_version": 1, "kind": "price_free_funding_preview", "as_of": as_of,
                "market": {key: config[key] for key in ("asset", "quote", "source", "data_mode")},
                "fees": config["fees"], "policies_equivalent": policies_equivalent(config),
                "families": {family: _preview_family(config, as_of, family)
                             for family in ("regular", "split", "deferred")},
                "future_contributions": [{**row} for row in config["contributions"] if row["time"] > as_of],
                "assumptions": ["Declared quote notional excludes quote fees; each positive slice incurs its own fixed fee.",
                    "No borrowing: deposits are available only at their declared timestamps.",
                    "Price-free cash commitments assume no fills or rounding refunds; this is not an account balance.",
                    "A deferred reserve cap can create an additional immediate purchase and additional fixed fee; allocated cash is unchanged and achievable notional can fall.",
                    "Base-asset fees reduce received units; their quantity cannot be estimated without a price.",
                    "Ledgerly remains authoritative for actual transactions and holdings."]}
