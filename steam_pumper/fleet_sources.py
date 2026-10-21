"""Acknowledged source generations for Fleet's dynamically selected engines."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .remote_sources import SourceListSnapshot


class FleetSources:
    def _init_source_state(self) -> None:
        namespace = hashlib.sha256(str(self.config_path.resolve()).encode()).hexdigest()[:16]
        self.sources_runtime_dir = Path(os.environ.get("FLEET_RUNTIME_DIR", "/tmp/fleet-sources")) / namespace
        self.active_source_revision = 0
        self.source_generation = self._source_generation(self.effective_source_pool, 0)
        self.pending_source_generation = ""
        self.pending_source_revision = 0
        self.pending_source_pool: list[str] = []
        self.source_apply_started = 0.0
        self.source_apply_retry_at = 0.0
        self.source_apply_attempts = 0
        self.account_source_health: dict[str, dict[str, Any]] = {}

    def _source_generation(self, sources: list[str], revision: int) -> str:
        content = json.dumps([sources, self.cfg.source_pool], separators=(",", ":"))
        return f"{revision}:{hashlib.sha256(content.encode()).hexdigest()[:16]}"

    def _begin_source_update(self, sources: list[str], revision: int, origin: str, at: float) -> None:
        generation = self._source_generation(sources, revision)
        self.effective_source_origin = origin
        if generation == self.source_generation and not self.pending_source_generation:
            self.active_source_revision = revision
            return
        self.pending_source_pool = list(sources)
        self.pending_source_revision = revision
        self.pending_source_generation = generation
        self.source_apply_attempts = 0
        self.source_apply_started = at
        self.source_apply_retry_at = at
        self._advance_source_update(at)

    def _poll_remote_sources(self, monotonic_now: float, now: datetime) -> None:
        if self.remote_source_refresher is not None:
            result = self.remote_source_refresher.poll_result()
            if isinstance(result, Exception):
                self.log("source-list refresh failed")
            elif result is not None:
                _changed, sources, snapshot = result
                self.remote_source_snapshot = snapshot
                if sources:
                    origin = "last-known-good" if snapshot.last_error else "remote"
                    generation = self._source_generation(sources, snapshot.revision)
                    if generation not in {self.source_generation, self.pending_source_generation}:
                        self._begin_source_update(list(sources), snapshot.revision, origin, monotonic_now)
                    else:
                        self.effective_source_origin = origin
            self.remote_source_refresher.request_if_due(now)
        self._advance_source_update(monotonic_now)

    def _advance_source_update(self, at: float) -> None:
        generation = self.pending_source_generation
        if not generation:
            return
        waiting = [line for line in self.active.values()
                   if not line.engine.source_generation_confirmed(generation)]
        if not waiting:
            self.effective_source_pool = list(self.pending_source_pool)
            self.active_source_revision = self.pending_source_revision
            self.source_generation = generation
            self.pending_source_generation = ""
            self.pending_source_pool = []
            self.pending_source_revision = 0
            self.log(f"source-list generation={generation} applied")
            return
        if at < self.source_apply_retry_at:
            return
        # Do not repeatedly cancel requests while a helper is acknowledging.
        attempted = False
        for line in waiting:
            engine = line.engine
            if (engine.state.pending_source_generation == generation
                    and not engine.state.source_reload_error
                    and at - self.source_apply_started < 30):
                continue
            attempted = True
            try:
                engine.fallback_sources = self._fallback_sources(self.pending_source_pool)
                engine.stage_sources(self.pending_source_pool, generation)
            except (OSError, ValueError) as exc:
                engine.state.source_reload_error = str(exc)[-500:]
        if attempted:
            self.source_apply_attempts += 1
            self.source_apply_retry_at = at + (5, 15, 60)[min(self.source_apply_attempts - 1, 2)]
        else:
            self.source_apply_retry_at = max(at, self.source_apply_started + 30)

    def _fallback_sources(self, primary: list[str]) -> list[str]:
        return [url for url in self.cfg.source_pool if url not in primary]

    def source_snapshot(self) -> list[dict[str, Any]]:
        urls = list(dict.fromkeys([*self.effective_source_pool, *self.pending_source_pool, *self.cfg.source_pool]))
        rows = []
        for url in urls:
            lines = []
            for account_id, active in self.active.items():
                state = active.engine.state.source_states.get(url)
                if state is not None:
                    lines.append({"line_id": account_id, **vars(state)})
            healthy = [item for item in lines if item["state"] == "healthy"]
            state = ("healthy" if healthy else "quarantined" if lines and all(
                item["state"] == "quarantined" for item in lines) else "unknown" if not lines or all(
                    item["state"] == "unknown" for item in lines) else "degraded")
            rows.append({"url": url, "ip": "", "healthy": bool(healthy) if lines else None,
                         "state": state, "failures": sum(item["consecutive_failures"] for item in lines),
                         "retry_in_seconds": min((item["retry_in_seconds"] for item in lines), default=0),
                         "last_error": next((item["last_error"] for item in lines if item["last_error"]), ""),
                         "fallback": url not in self.effective_source_pool, "lines": lines})
        return rows

    def source_list_status(self) -> dict[str, Any]:
        status = self.remote_source_snapshot.to_dict() if self.remote_source_manager is not None else {
            **SourceListSnapshot(enabled=False, status="local").to_dict(),
            "source_count": len(self.effective_source_pool),
        }
        now = time.monotonic()
        generation = self.pending_source_generation or self.source_generation
        lines = []
        for account_id, active in self.active.items():
            engine = active.engine
            confirmed = engine.source_generation_confirmed(generation)
            error = engine.state.source_reload_error
            requested_at = engine.state.source_reload_started_at or self.source_apply_started
            if not confirmed and not error and requested_at and now - requested_at >= 30:
                error = "source reload acknowledgement timed out"
            if not confirmed and engine.state.status == "restarting":
                error = error or engine.state.last_error
            lines.append({"line_id": account_id, "generation": engine.state.confirmed_source_generation,
                          "pending_generation": engine.state.pending_source_generation,
                          "apply_state": "applied" if confirmed else "failed" if error else "pending",
                          "error": error})
        if not self.pending_source_generation and all(line["apply_state"] == "applied" for line in lines):
            apply_state = "applied"
        elif any(line["apply_state"] == "applied" for line in lines):
            apply_state = "partial"
        elif any(line["apply_state"] == "failed" for line in lines):
            apply_state = "failed"
        else:
            apply_state = "pending"
        status.update(origin=self.effective_source_origin, active_revision=self.active_source_revision or None,
                      active_generation=self.source_generation, pending_revision=self.pending_source_revision or None,
                      pending_generation=self.pending_source_generation, apply_state=apply_state,
                      apply_error="; ".join(f"{line['line_id']}: {line['error']}" for line in lines if line["error"]),
                      refresh_state=self.remote_source_refresher.status() if self.remote_source_refresher else "disabled",
                      lines=lines)
        return status

    def _completion_diagnostics(self, now: datetime) -> list[dict[str, Any]]:
        forecasts = self._cached_view("forecast", now)
        router = self._router_snapshot(now)
        groups = {group.group_id: group for group in self.store.groups()}
        remaining_day = max(0.0, (now.replace(hour=23, minute=59, second=59, microsecond=0) - now).total_seconds() + 1)
        daytime = max(0.0, (now.replace(hour=18, minute=0, second=0, microsecond=0) - now).total_seconds())
        runnable = daytime + min(3600.0, remaining_day)
        rows = []
        phase = self._daily_phase(now)
        if phase == "upload_only":
            rows.append({"line_id": "*", "reason": "upload_only", "message": "18:00–23:00 上传保护：下载暂停，23:00 恢复补量"})
        eligible = [account for account in self.store.accounts() if account.enabled and account.profile_confirmed
                    and self.snmp_quality.get(account.account_id, "pending") not in {"pending", "baseline", "offline", "missing", "ambiguous", "reboot_gap"}
                    and account.account_id not in self.account_cooldowns]
        debt_total = sum(forecasts.get(account.account_id, {}).get("remaining_download_bytes", 0) for account in eligible)
        # An optimistic upper bound is sufficient to prove an impossible goal.
        group_capacity = sum(min(group.download_limit_mbps,
                                 sum(sorted((a.download_mbps for a in eligible if a.group_id == group.group_id), reverse=True)[:group.max_active]))
                             for group in groups.values() if group.enabled)
        profile_capacity = sum(sorted((a.download_mbps for a in eligible), reverse=True)[:20])
        day_rate = min(router["available_mbps"], group_capacity, profile_capacity)
        late_rate = min(day_rate, self.cfg.late_download_limit_mbps)
        maximum_bytes = int((day_rate * daytime + late_rate * min(3600.0, remaining_day)) * 1_000_000 / 8)
        if router["status"] == "ok" and debt_total > maximum_bytes:
            rows.append({"line_id": "*", "reason": "node_capacity_shortfall",
                         "message": f"剩余时段理论容量不足，至少缺 {max(0, debt_total - maximum_bytes) / 1e9:.1f} GB；需提前完成白天下载",
                         "remaining_download_bytes": debt_total, "available_download_seconds": runnable,
                         "maximum_download_bytes": maximum_bytes, "shortfall_bytes": debt_total - maximum_bytes})
        for account in self.store.accounts():
            debt = forecasts.get(account.account_id, {}).get("remaining_download_bytes", 0)
            if not account.enabled or debt <= 0:
                continue
            active = self.active.get(account.account_id)
            measured = self._active_rate(active, 60) if active and len(active.rate_samples) > 1 else None
            estimated = debt * 8 / measured / 1_000_000 if measured and measured > 0 else None
            theoretical = debt * 8 / account.download_mbps / 1_000_000
            reason, message = "", ""
            if not account.profile_confirmed:
                reason, message = "profile_unconfirmed", "账号带宽档案未确认，下载暂停"
            elif not self.manual_enabled:
                reason, message = "manual_stop", "节点已手动停止下载"
            elif account.account_id in self.account_cooldowns:
                reason, message = "cooldown", "线路处于故障冷却"
            elif router["status"] != "ok" or self.snmp_quality.get(account.account_id, "pending") in {
                "pending", "baseline", "offline", "missing", "ambiguous", "reboot_gap"
            }:
                reason, message = "snmp_unavailable", "SNMP 数据尚不可用于调度"
            elif theoretical > runnable:
                reason, message = "insufficient_time", "即使按额定档位下载，剩余窗口也不足以完成基础目标"
            elif (groups.get(account.group_id) is None or not groups[account.group_id].enabled
                  or groups[account.group_id].download_limit_mbps < account.download_mbps
                  or (self._daily_phase(now) != "upload_only" and router["phase_limit_mbps"] < account.download_mbps)):
                reason, message = "insufficient_budget", "当前预算不足一个完整线路档位或分组已禁用"
            elif estimated is not None and estimated > runnable:
                reason, message = "source_throughput", "按最近实测吞吐预计无法在剩余窗口完成基础目标"
            if reason:
                rows.append({"line_id": account.account_id, "reason": reason, "message": message,
                             "remaining_download_bytes": debt, "available_download_seconds": runnable,
                             "estimated_completion_seconds": estimated})
            unknown = forecasts.get(account.account_id, {}).get("unknown_upload_budget_bytes", 0)
            if unknown:
                rows.append({"line_id": account.account_id, "reason": "unknown_measurement_budget",
                             "message": f"计量缺失带来的保守上传预算 {unknown / 1e9:.1f} GB，目标包含未知预算，需核对 SNMP",
                             "unknown_upload_budget_bytes": unknown})
        return rows
