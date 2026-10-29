"""Bounded native derivatives comparisons from explicitly supplied local evidence.

Each historical endpoint uses only observations available by that endpoint's
cutoff. Different venues, quotes, contracts, counting conventions, funding kinds
and native periods remain separate. This module performs no collection or FX.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict

from .evidence import canonical_json, funding_period_seconds, metric_dimension
from .models import Observation

MAX_OBSERVATIONS = 5000
MAX_SERIES = 64
METRICS = {'funding_rate_actual', 'funding_rate_projected', 'open_interest_amount', 'open_interest_value'}
DEFAULT_POLICY = {'max_point_age_seconds': 3600, 'endpoint_tolerance_seconds': 900}
_UNKNOWN = {'', 'unknown', 'unspecified', 'none', 'contracts_unknown'}
_CURRENCIES = {'EUR', 'USD', 'USDT', 'USDC', 'BTC', 'ETH'}


def _time(value, name):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f'{name} must be a nonnegative integer UTC epoch second')
    return value


def _text(value, name, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError(f'{name} must be an explicit bounded string')
    return value


def _known(value):
    return isinstance(value, str) and value.strip().lower() not in _UNKNOWN


def _number(value):
    return type(value) in (int, float) and -1e100 <= value <= 1e100 and math.isfinite(value)


def _metadata_number(text):
    value = float(text) if any(char in text for char in '.eE') else int(text)
    if not _number(value):
        raise ValueError('Metadata numbers exceed the finite supported range')
    return value


def _validate(request):
    allowed = {'schema_version', 'asset', 'data_mode', 'source', 'quote', 'policy', 'observations'}
    if (not isinstance(request, dict) or type(request.get('schema_version')) is not int
            or request['schema_version'] != 1 or set(request) - allowed):
        raise ValueError('A derivatives context request requires schema_version 1 and supported fields')
    if request.get('asset') not in ('BTC', 'ETH') or request.get('data_mode') not in ('live', 'import', 'demo'):
        raise ValueError('Explicit BTC/ETH asset and live, import or demo data_mode are required')
    selectors = {key: _text(request.get(key), key, nullable=True) for key in ('source', 'quote')}
    policy = request.get('policy', {})
    if not isinstance(policy, dict) or set(policy) - DEFAULT_POLICY.keys():
        raise ValueError('Unsupported derivatives context policy fields')
    policy = {**DEFAULT_POLICY, **policy}
    for key, low, high in [('max_point_age_seconds', 1, 86400), ('endpoint_tolerance_seconds', 0, 3600)]:
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}')
    rows = request.get('observations')
    if not isinstance(rows, list) or len(rows) > MAX_OBSERVATIONS:
        raise ValueError(f'observations must contain at most {MAX_OBSERVATIONS} local rows')
    observations = []
    fields = {'source', 'asset', 'metric', 'event_time', 'observed_at', 'value', 'unit', 'quote', 'metadata', 'data_mode'}
    for row in rows:
        if not isinstance(row, dict) or set(row) - fields:
            raise ValueError('Observations must use the public Observation fields')
        for key in ('source', 'asset', 'metric'):
            _text(row.get(key), f'observation.{key}')
        if row.get('unit') is not None and (not isinstance(row['unit'], str) or len(row['unit']) > 120):
            raise ValueError('observation.unit must be a bounded native string or null')
        if row.get('data_mode') not in ('live', 'import', 'demo'):
            raise ValueError('Every observation requires its explicit data_mode')
        timestamps = {}
        for key in ('event_time', 'observed_at'):
            value = row.get(key)
            timestamps[key] = None if value is None else _time(value, f'observation.{key}')
        value = row.get('value')
        if value is not None and not _number(value):
            raise ValueError('Observation values must be finite native numbers within 1e100 or null')
        metadata = row.get('metadata', {})
        if not isinstance(metadata, dict):
            raise ValueError('Observation metadata must be a JSON object')
        try:
            encoded = canonical_json(metadata)
            metadata = json.loads(encoded, parse_int=_metadata_number, parse_float=_metadata_number)
        except (TypeError, ValueError) as exc:
            raise ValueError('Observation metadata must contain finite JSON values') from exc
        if len(encoded.encode()) > 4096:
            raise ValueError('Observation metadata exceeds the 4096-byte bound')
        observations.append(Observation(
            row['source'], row['asset'], row['metric'], timestamps['event_time'], timestamps['observed_at'],
            value, row.get('unit'), _text(row.get('quote'), 'observation.quote', nullable=True),
            metadata, row['data_mode']))
    return selectors, policy, observations


def derivative_dimension(observation):
    dimension = metric_dimension(observation)
    # Contract-size units and native value currency are essential even when two
    # providers happen to publish the same numeric contract size or OI amount.
    for key in ('contract_size_unit', 'value_currency', 'funding_kind', 'projected'):
        if key in observation.metadata:
            dimension[key] = observation.metadata[key]
    return dimension


def _native_status(observation):
    metadata = observation.metadata
    if not all(_known(value) for value in (observation.quote, observation.unit,
                                          metadata.get('symbol'), metadata.get('settle'))):
        return 'unknown_native_convention'
    if observation.metric.startswith('funding_rate_'):
        kind = 'actual' if observation.metric == 'funding_rate_actual' else 'projected'
        if (observation.unit != 'rate' or funding_period_seconds(metadata) is None
                or any(metadata.get(key, kind) != kind for key in ('value_kind', 'funding_kind'))
                or ('projected' in metadata and metadata['projected'] is not (kind == 'projected'))):
            return 'unknown_native_convention'
    else:
        if not _known(metadata.get('counting_convention')):
            return 'unknown_native_convention'
        if observation.unit == 'contracts':
            size = metadata.get('contract_size')
            if (not _number(size) or size <= 0
                    or metadata.get('contract_size_unit') not in _CURRENCIES):
                return 'unknown_native_convention'
        elif observation.unit not in _CURRENCIES:
            return 'unknown_native_convention'
        elif 'value_currency' in metadata and metadata['value_currency'] != observation.unit:
            return 'unknown_native_convention'
    return 'known'


def _point_status(point, age, policy):
    if age >= policy['max_point_age_seconds'] or point.metadata.get('status') in ('stale', 'expired'):
        return 'stale'
    if _native_status(point) != 'known':
        return 'unknown_native_convention'
    if point.metadata.get('timestamp_source') in ('observation', 'unknown'):
        return 'unknown_timestamp'
    if point.metadata.get('historical_known_at') == 'unknown' or point.metadata.get('vintage') == 'current':
        return 'unknown_availability'
    if point.value is None or (point.metric.startswith('open_interest_') and point.value < 0):
        return 'unknown_value'
    if point.metadata.get('status', 'observed') not in ('observed', 'ok', 'available', 'complete'):
        return 'unknown_observation_status'
    return 'ok'


def _endpoint(observations, cutoff, policy):
    eligible = [row for row in observations if row.event_time is not None and row.observed_at is not None
                and row.event_time <= cutoff and row.observed_at <= cutoff]
    if not eligible:
        return {'status': 'missing_endpoint', 'point': None, 'point_age_seconds': None}
    event_time = max(row.event_time for row in eligible)
    observed_at = max(row.observed_at for row in eligible if row.event_time == event_time)
    latest = [row for row in eligible if row.event_time == event_time and row.observed_at == observed_at]
    if len({canonical_json(row) for row in latest}) != 1:
        return {'status': 'unknown_conflicting_versions', 'point': None, 'point_age_seconds': cutoff - event_time}
    point = latest[0]
    age = cutoff - point.event_time
    return {'status': _point_status(point, age, policy), 'point': {**asdict(point), 'event_age_seconds': age,
                                                                'ingestion_age_seconds': cutoff - point.observed_at},
            'point_age_seconds': age}


def _comparison(observations, as_of, horizon, policy):
    start = _endpoint(observations, as_of - horizon, policy)
    end = _endpoint(observations, as_of, policy)
    status = 'ok'
    if end['status'] != 'ok':
        status = f"end_{end['status']}"
    elif start['status'] != 'ok':
        status = f"start_{start['status']}"
    elif max(start['point_age_seconds'], end['point_age_seconds']) > policy['endpoint_tolerance_seconds']:
        status = 'endpoint_outside_tolerance'
    elapsed = end['point']['event_time'] - start['point']['event_time'] if start['point'] and end['point'] else None
    return {'status': status, 'horizon_seconds': horizon, 'start_cutoff': as_of - horizon, 'end_cutoff': as_of,
            'start': start['point'], 'end': end['point'], 'start_status': start['status'], 'end_status': end['status'],
            'start_point_age_seconds': start['point_age_seconds'], 'end_point_age_seconds': end['point_age_seconds'],
            'elapsed_seconds': elapsed, 'endpoint_skew_seconds': elapsed - horizon if elapsed is not None else None,
            'native_change': end['point']['value'] - start['point']['value'] if status == 'ok' else None,
            'change_unit': end['point']['unit'] if end['point'] else None}


def analyze_derivatives_context(request: dict, as_of: int) -> dict:
    """Describe separate native funding/OI series and known-at 4h/24h changes.

    Schema v1 requires asset, data_mode and up to 5000 Observation-shaped JSON
    rows. Optional source and quote selectors filter without conversion. Policy
    controls maximum point age (default 3600 seconds) and how far *before* each
    endpoint a point may fall (default 900 seconds, at most 3600). No point after
    an endpoint is substituted. Both event_time and observed_at must be known
    and at or before that endpoint. Funding periods use the shared normalizer.

    OI requires an explicit counting convention, native unit, symbol and settle;
    contracts additionally require positive contract_size and contract_size_unit.
    Unknown timestamps, conventions, equal-vintage conflicts and missing history
    cannot establish a change. The result does not qualify a provider or trade.
    """
    as_of = _time(as_of, 'as_of')
    selectors, policy, observations = _validate(request)
    groups = {}
    excluded = {'outside_selection': 0, 'unsupported_metric': 0, 'after_as_of': 0}
    for row in observations:
        if (row.asset != request['asset'] or row.data_mode != request['data_mode']
                or any(selectors[key] is not None and getattr(row, key) != selectors[key] for key in selectors)):
            excluded['outside_selection'] += 1
            continue
        if row.metric not in METRICS:
            excluded['unsupported_metric'] += 1
            continue
        if ((row.event_time is not None and row.event_time > as_of)
                or (row.observed_at is not None and row.observed_at > as_of)):
            excluded['after_as_of'] += 1
            continue
        identity = (row.source, row.asset, row.metric, row.data_mode, canonical_json(derivative_dimension(row)))
        groups.setdefault(identity, []).append(row)
        if len(groups) > MAX_SERIES:
            raise ValueError(f'At most {MAX_SERIES} separate native derivatives series are supported')
    series = []
    for identity, rows in sorted(groups.items()):
        current = _endpoint(rows, as_of, policy)
        unknown_times = sum(row.event_time is None or row.observed_at is None
                            or row.metadata.get('timestamp_source') in ('observation', 'unknown') for row in rows)
        status = ('unknown_timestamp' if current['status'] == 'missing_endpoint' and unknown_times else current['status'])
        series.append({'identity': {'source': identity[0], 'asset': identity[1], 'metric': identity[2],
                                    'data_mode': identity[3], 'dimension': json.loads(identity[4])},
                       'status': status, 'current': current['point'],
                       'unknown_timestamp_observations': unknown_times,
                       'comparisons': {label: _comparison(rows, as_of, seconds, policy)
                                       for label, seconds in [('4h', 14400), ('24h', 86400)]}})
    return {'schema_version': 1, 'kind': 'native_derivatives_context', 'as_of': as_of,
            'asset': request['asset'], 'data_mode': request['data_mode'], **selectors, 'policy': policy,
            'series': series, 'excluded': excluded,
            'limitations': [
                'Explicit local observations only; provider availability, native definitions and historical coverage are not independently verified.',
                'Sources, native quotes, units, symbols, settlements, contract sizes and counting conventions are never pooled or converted.',
                'Actual and projected funding and different native funding periods remain independent; changes are native rate differences, never annualized yields.',
                'Historical endpoints require event and observation times at or before that cutoff; later revisions cannot rewrite an earlier endpoint.',
                'Points must be fresh and within the stated backward endpoint tolerance; missing endpoints or unknown conventions leave changes unknown.',
                'Open interest describes positioning, not spot demand, leverage, liquidation levels or a crash signal.',
            ]}
