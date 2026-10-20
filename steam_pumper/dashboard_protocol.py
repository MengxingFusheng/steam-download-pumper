"""Version 1 Fleet dashboard contract, shared without cloud dependencies."""
from __future__ import annotations

import hashlib
import json

SCHEDULE_FIELDS = frozenset({
    'target_ratio', 'rotation_minutes',
    'router_limit_mbps', 'router_reserve_mbps', 'late_download_limit_mbps',
    'line_stall_seconds', 'line_slow_window_seconds',
    'line_failure_cooldown_minutes',
})


def schedule_config(cfg):
    values = cfg.to_dict() if hasattr(cfg, 'to_dict') else cfg
    return {key: values[key] for key in sorted(SCHEDULE_FIELDS)}


def config_version(cfg):
    return hashlib.sha256(json.dumps(schedule_config(cfg), sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def validate_command(action, params):
    if action not in {'start', 'stop', 'update_schedule'}:
        raise ValueError('unsupported command')
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    if action != 'update_schedule' and params:
        raise ValueError('start/stop do not accept parameters')
    if action == 'update_schedule' and (not params or set(params) - SCHEDULE_FIELDS):
        raise ValueError('unsupported or empty schedule parameters')
