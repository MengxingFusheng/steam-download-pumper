"""Bounded, explicit telemetry contract. Unknown fields fail closed."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Totals(Model):
    actual_bytes: int = Field(ge=0, le=10**18)
    program_bytes: int = Field(ge=0, le=10**18)
    measured_bytes: int = Field(ge=0, le=10**18)
    estimated_bytes: int = Field(ge=0, le=10**18)
    legacy_bytes: int = Field(ge=0, le=10**18)
    unknown_seconds: int = Field(ge=0)
    target_bytes: int = Field(ge=0, le=10**18)


class Summary(Totals):
    remaining_bytes: int = Field(ge=0, le=10**18)
    upload_bytes: int | None = Field(default=None, ge=0, le=10**18)
    upload_mbps: float | None = Field(default=None, ge=0)
    minimum_accept_bytes: int | None = Field(default=None, ge=0, le=10**18)


class Line(Totals):
    line_id: str = Field(min_length=1, max_length=200)
    bind_ip: str = Field(max_length=45)
    snmp_interface: str = Field(max_length=200)
    enabled: bool
    active: bool
    weight: int | None
    group_id: str = Field(max_length=200)
    upload_mbps: float | None = Field(default=None, ge=0)
    upload_bytes: int | None = Field(default=None, ge=0, le=10**18)
    current_upload_mbps: float | None = Field(default=None, ge=0)
    download_mbps: float = Field(ge=0)
    assigned_mbps: float = Field(ge=0)
    status: str = Field(max_length=100)
    avg60_mbps: float = Field(ge=0)
    profile_confirmed: bool
    forecast_quality: str | None = Field(max_length=100)
    metrics_available: bool | None = None


class PeriodLine(Model):
    line_id: str = Field(min_length=1, max_length=200)
    actual_bytes: int = Field(ge=0, le=10**18)
    program_bytes: int = Field(ge=0, le=10**18)
    unknown_seconds: int = Field(ge=0)
    cumulative_actual_bytes: int = Field(ge=0, le=10**18)
    target_bytes: int | None = Field(ge=0, le=10**18)
    target_recorded_at: str | None = Field(max_length=100)
    config_version: str | None = Field(max_length=64)
    completion: Literal['not_applicable', 'pending', 'unknown', 'no_target', 'achieved', 'unmet']


class Period(Model):
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    mode: Literal['daily', 'legacy', 'multi_ip']
    phase: Literal['daytime', 'upload_only', 'late_makeup', 'legacy_00_01', 'legacy_01_07', 'legacy_07_18', 'multi_ip_window']
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    state: Literal['pending', 'closed', 'active']
    evening_override: list[str] = Field(max_length=1500)
    lines: list[PeriodLine] = Field(max_length=1500)


class Rolling(Model):
    actual_bytes: int = Field(ge=0, le=10**18)
    target_bytes: int = Field(ge=0, le=10**18)
    days: int = Field(ge=1)


class SourceLine(Model):
    line_id: str = Field(min_length=1, max_length=200)
    generation: str | None = Field(default=None, max_length=128)
    pending_generation: str | None = Field(default=None, max_length=128)
    apply_state: Literal['applied', 'pending', 'partial', 'failed']
    error: str = Field(default='', max_length=2000)


class SourceList(Model):
    enabled: bool
    status: str = Field(max_length=100)
    revision: int | None = Field(default=None, ge=0)
    active_revision: int | None = Field(default=None, ge=0)
    pending_revision: int | None = Field(default=None, ge=0)
    pending_generation: str | None = Field(default=None, max_length=128)
    apply_state: Literal['applied', 'pending', 'partial', 'failed']
    apply_error: str = Field(default='', max_length=2000)
    source_count: int = Field(ge=0)
    origin: str = Field(max_length=100)
    stale: bool = False
    last_success_at: str | None = Field(default=None, max_length=100)
    next_refresh_at: str | None = Field(default=None, max_length=100)
    last_error: str = Field(default='', max_length=2000)
    lines: list[SourceLine] = Field(default_factory=list, max_length=1500)


class Snapshot(Model):
    schema_version: Literal[1]
    observed_at: int = Field(ge=0)
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    timezone: Literal['Asia/Shanghai']
    mode: Literal['daily', 'legacy', 'multi_ip']
    measurement_basis: Literal['snmp', 'program'] = 'snmp'
    measurement_started_at: int | None = Field(default=None, ge=0)
    target_basis: Literal['upload_ratio', 'configured_speed'] = 'upload_ratio'
    capabilities: list[Literal['start', 'stop', 'update_schedule']] = Field(default_factory=lambda: ['start', 'stop', 'update_schedule'], max_length=3)
    stats_source: Literal['program', 'interface_fallback'] | None = None
    manual_enabled: bool
    snmp_status: str = Field(max_length=100)
    config: dict
    config_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    summary: Summary
    lines: list[Line] = Field(max_length=1500)
    groups: list[dict] = Field(max_length=1500)
    router_budget: dict
    periods: list[Period] = Field(max_length=16)
    rolling: Rolling | None = None
    source_list: SourceList | None = None
    diagnostics: list[dict] = Field(default_factory=list, max_length=1500)

    @model_validator(mode='after')
    def classic_accounting(self):
        if self.mode == 'multi_ip':
            if self.measurement_basis != 'program' or self.target_basis != 'configured_speed':
                raise ValueError('multi_ip snapshots require program accounting and configured-speed targets')
            for item in [self.summary, *self.lines]:
                if (item.actual_bytes != item.program_bytes or item.measured_bytes or item.estimated_bytes
                        or item.legacy_bytes or item.upload_bytes is not None):
                    raise ValueError('multi_ip snapshots cannot claim SNMP accounting or upload totals')
            if self.summary.upload_mbps is not None or any(line.current_upload_mbps is not None for line in self.lines):
                raise ValueError('multi_ip snapshots cannot claim upload rates')
        return self
