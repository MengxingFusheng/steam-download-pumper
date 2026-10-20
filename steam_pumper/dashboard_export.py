"""Export existing Fleet accounting; called only from the controller thread."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .dashboard_protocol import config_version, schedule_config

SHANGHAI = ZoneInfo('Asia/Shanghai')


def current_upload_rates(controller, now):
    """Latest accepted SNMP counter deltas; missing or stale samples stay unknown.

    Fleet's existing line.upload_mbps describes the account's rated capacity.
    Keep that contract and retain only counter baselines here for live rates.
    Fleet calls this after every SNMP observation, before the slower dashboard
    export can skip a sample. Exports reuse the latest rate while it is fresh.
    """
    if controller.snmp_status != 'online':
        controller._dashboard_upload_samples = {}
        return {}
    samples = getattr(controller, '_dashboard_upload_samples', {})
    next_samples = {}
    rates = {}
    max_age = max(120, controller.cfg.snmp_poll_seconds * 2)
    swap = controller.cfg.snmp_direction_swap
    for record in controller.store.db.execute('SELECT * FROM cursors'):
        sample = dict(record)
        account_id = sample['account_id']
        sample['swap'] = swap
        sample['rate'] = None
        fresh = 0 <= now.timestamp() - sample['sampled_at'] <= max_age
        valid = bool(sample['valid']) and sample['missing_since'] is None and sample['oper_status'] in (None, 1)
        previous = samples.get(account_id)
        quality = controller.snmp_quality.get(account_id)
        column = 'in_octets' if swap else 'out_octets'
        if fresh and valid and previous and previous['swap'] == swap:
            if sample['sampled_at'] == previous['sampled_at']:
                if (quality == 'ok' and sample['if_index'] == previous['if_index']
                        and sample['discontinuity'] == previous['discontinuity']
                        and sample[column] == previous[column]):
                    sample['rate'] = previous['rate']
            else:
                elapsed = sample['sampled_at'] - previous['sampled_at']
                unchanged = (sample['if_index'] == previous['if_index']
                             and sample['discontinuity'] == previous['discontinuity']
                             and (sample['device_uptime'] is None or previous['device_uptime'] is None
                                  or sample['device_uptime'] >= previous['device_uptime']))
                if (quality == 'ok' and previous['valid'] and unchanged and 0 < elapsed <= max_age
                        and sample[column] is not None and previous[column] is not None
                        and sample[column] >= previous[column]):
                    sample['rate'] = (sample[column] - previous[column]) * 8 / elapsed / 1_000_000
        next_samples[account_id] = sample
        rates[account_id] = sample['rate']
    controller._dashboard_upload_samples = next_samples
    return rates


def traffic_buckets(controller, lower, upper):
    """Closed ten-minute buckets. Missing samples remain missing, not zero."""
    rows = controller.store.db.execute('''
        SELECT account_id, CAST(minute/10 AS INTEGER)*600 AS bucket,
          SUM(legacy_download) AS legacy_bytes, SUM(measured_download_bytes) AS measured_bytes,
          SUM(estimated_download_bytes) AS estimated_bytes, SUM(download_bytes) AS program_bytes,
          SUM(unknown_seconds) AS unknown_seconds, SUM(sample_seconds) AS sample_seconds,
          COUNT(*) AS observed_minutes
        FROM (SELECT *, snmp_download_bytes AS legacy_download FROM traffic)
        WHERE minute>=? AND minute<? GROUP BY account_id, bucket
        ORDER BY bucket, account_id
    ''', (lower // 60, upper // 60)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['line_id'] = item.pop('account_id')
        item['start'] = item.pop('bucket')
        item['actual_bytes'] = item['legacy_bytes'] + item['measured_bytes'] + item['estimated_bytes']
        # sample_seconds covers accepted ledger intervals; unknowns are separate.
        item['quality'] = 'unknown' if item['unknown_seconds'] else 'estimated' if item['estimated_bytes'] else 'measured' if item['sample_seconds'] or item['legacy_bytes'] else 'program_only'
        result.append(item)
    return result


def period_views(controller, now, targets):
    definitions = [('daytime', 0, 18), ('upload_only', 18, 23), ('late_makeup', 23, 24)]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    date = now.date().isoformat()
    mode = 'daily'
    forecast = controller.store.daily_forecasts(now)
    pre18 = json.loads(controller.store._meta(f'daily_pre18:{date}') or '{}')
    periods = []
    for phase, left, right in definitions:
        start, end = midnight + timedelta(hours=left), midnight + timedelta(hours=right)
        actual = controller.store._totals_between(int(start.timestamp() // 60), int(min(end, now).timestamp() // 60)) if now >= start else {}
        cumulative = controller.store._totals_between(int(midnight.timestamp() // 60), int(min(end, now).timestamp() // 60)) if now >= start else {}
        key = f'{date}:{mode}:{phase}'
        phase_target = targets.get(key, {})
        if start <= now < end:
            phase_target = {account.account_id: {
                'target_bytes': int(forecast[account.account_id]['target_download_bytes']),
                'recorded_at': now.isoformat(), 'config_version': config_version(controller.cfg),
            } for account in controller.store.accounts()}
            targets[key] = phase_target
        lines = []
        for account in controller.store.accounts():
            value = actual.get(account.account_id, {})
            goal = phase_target.get(account.account_id, {})
            target = goal.get('target_bytes')
            if phase == 'daytime' and now >= end:
                target = pre18.get(account.account_id, target)
            no_target = phase == 'upload_only'
            down = cumulative.get(account.account_id, {}).get('download', 0)
            lines.append({'line_id': account.account_id, 'actual_bytes': value.get('download', 0),
                          'program_bytes': value.get('program_download', 0), 'unknown_seconds': value.get('unknown_seconds', 0),
                          'cumulative_actual_bytes': down, 'target_bytes': None if no_target else target,
                          'target_recorded_at': goal.get('recorded_at'), 'config_version': goal.get('config_version'),
                          'completion': 'not_applicable' if no_target else 'pending' if now < end else 'unknown' if target is None else 'no_target' if target == 0 else 'achieved' if down >= target else 'unmet'})
        periods.append({'date': date, 'mode': mode, 'phase': phase, 'start': int(start.timestamp()),
                        'end': int(end.timestamp()), 'state': 'pending' if now < start else 'closed' if now >= end else 'active',
                        'evening_override': (controller._evening_override(now) or {}).get('account_ids', []) if phase == 'upload_only' else [],
                        'lines': lines})
    return periods


def export_snapshot(controller, now, targets):
    # Explicit projection: never send raw config, logs, sources or environment.
    totals = controller.store.today(now)
    forecasts = controller.store.daily_forecasts(now)
    metrics = controller.metrics()
    upload_rates = current_upload_rates(controller, now)
    lines = []
    for line in metrics['lines']:
        account_id = line['account_id']
        actual = totals[account_id]
        prediction = forecasts[account_id]
        lines.append({key: line.get(key) for key in (
            'line_id', 'bind_ip', 'snmp_interface', 'enabled', 'active', 'weight', 'group_id',
            'upload_mbps', 'download_mbps', 'assigned_mbps', 'status', 'avg60_mbps', 'profile_confirmed')})
        lines[-1].update(actual_bytes=actual['download'], upload_bytes=actual['upload'],
                         current_upload_mbps=upload_rates.get(account_id), program_bytes=actual['program_download'],
                         measured_bytes=actual['measured_download'], estimated_bytes=actual['estimated_download'],
                         legacy_bytes=actual['legacy_download'], unknown_seconds=actual['unknown_seconds'],
                         target_bytes=int(prediction['target_download_bytes']),
                         forecast_quality=prediction.get('forecast_quality'))
    enabled = [line for line in lines if line['enabled']]
    summary = {key: sum(line[key] for line in enabled) for key in (
        'actual_bytes', 'upload_bytes', 'program_bytes', 'measured_bytes', 'estimated_bytes', 'legacy_bytes', 'unknown_seconds', 'target_bytes')}
    summary['remaining_bytes'] = sum(max(0, line['target_bytes'] - line['actual_bytes']) for line in enabled)
    summary['upload_mbps'] = (sum(line['current_upload_mbps'] for line in enabled)
                              if all(line['current_upload_mbps'] is not None for line in enabled) else None)
    return {'schema_version': 1, 'observed_at': int(now.timestamp()), 'date': now.date().isoformat(),
            'measurement_started_at': int(controller.store._meta('snmp_actual_schema_at')),
            'timezone': 'Asia/Shanghai', 'mode': 'daily',
            'manual_enabled': controller.manual_enabled, 'snmp_status': controller.snmp_status,
            'config': schedule_config(controller.cfg), 'config_version': config_version(controller.cfg),
            'summary': summary, 'lines': lines, 'groups': controller.groups_snapshot(),
            'router_budget': controller._router_snapshot(now), 'periods': period_views(controller, now, targets),
            'source_list': source_list_view(controller.source_list_status()),
            'diagnostics': metrics.get('diagnostics', []),
            'rolling': {'actual_bytes': metrics['rolling_download_bytes'], 'target_bytes': metrics['rolling_target_download_bytes'],
                        'days': controller.cfg.rolling_days}}


def source_list_view(status):
    """Project application status without leaking source URLs or fetch credentials."""
    fields = ('enabled', 'status', 'revision', 'active_revision', 'pending_revision',
              'pending_generation', 'apply_state', 'apply_error', 'source_count',
              'origin', 'stale', 'last_success_at', 'next_refresh_at', 'last_error')
    result = {key: status[key] for key in fields if key in status}
    for key in ('apply_error', 'last_error'):
        if key in result:
            result[key] = str(result[key])[:2000]
    result['lines'] = [{key: line[key] for key in (
        'line_id', 'generation', 'pending_generation', 'apply_state', 'error') if key in line}
        for line in status.get('lines', [])]
    return result
