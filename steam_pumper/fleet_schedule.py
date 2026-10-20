"""Daily allocation and approximate WAN budget, independent of download I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


def daily_phase(now: datetime) -> str:
    if now.hour < 18:
        return "daytime"
    if now.hour < 23:
        return "upload_only"
    return "late_makeup"


@dataclass
class WanBudget:
    interface: str = "wan1"
    baseline: tuple[Any, datetime, int | None] | None = None
    valid_at: datetime | None = None
    down_mbps: float = 0.0
    status: str = "pending"

    def observe(self, interfaces: Iterable[Any], at: datetime, uptime: int | None, *, swap: bool, stale_seconds: int) -> None:
        matches = [item for item in interfaces if item.name == self.interface or item.alias == self.interface]
        live = [item for item in matches if item.oper_status == 1]
        if len(live) == 1:
            matches = live
        if len(matches) != 1 or matches[0].oper_status not in (None, 1):
            self.baseline = None
            self.valid_at = None
            self.status = "missing" if not matches else "unavailable"
            return
        item = matches[0]
        previous = self.baseline
        self.baseline = (item, at, uptime)
        if previous is None:
            self.valid_at = None
            self.status = "baseline"
            return
        old, before, old_uptime = previous
        elapsed = (at - before).total_seconds()
        current = item.out_octets if swap else item.in_octets
        prior = old.out_octets if swap else old.in_octets
        reset = (
            old.index != item.index or old.discontinuity != item.discontinuity
            or current < prior
            or (uptime is not None and old_uptime is not None and uptime < old_uptime)
        )
        if reset or not 0 < elapsed <= stale_seconds:
            self.valid_at = None
            self.status = "reset" if reset else "baseline"
            return
        rate = (current - prior) * 8 / elapsed / 1_000_000
        # Physical 10G validation is deliberately independent of the 5G policy.
        if rate > 10_000 * 1.05:
            self.valid_at = None
            self.status = "anomaly"
            return
        self.down_mbps = rate
        self.valid_at = at
        self.status = "ok"

    def snapshot(self, now: datetime, fleet_avg60: float, cfg: Any) -> dict[str, Any]:
        valid = self.valid_at is not None and 0 <= (now - self.valid_at).total_seconds() <= cfg.wan_stale_seconds
        status = self.status if valid or self.valid_at is None else "stale"
        other = max(0.0, self.down_mbps - fleet_avg60) if valid else 0.0
        available = max(0.0, cfg.router_limit_mbps - other - cfg.router_reserve_mbps) if valid else 0.0
        phase = daily_phase(now)
        phase_limit = available if phase == "daytime" else min(cfg.late_download_limit_mbps, available) if phase == "late_makeup" else 0.0
        return {
            "interface": self.interface, "status": status,
            "wan_down_mbps": self.down_mbps if valid else None,
            "fleet_avg60_mbps": fleet_avg60, "other_down_mbps": other,
            "available_mbps": available, "phase_limit_mbps": phase_limit,
            "last_sample_at": self.valid_at.isoformat(timespec="seconds") if self.valid_at else None,
        }


def allocate_daily(
    accounts: Iterable[Any], groups: Iterable[Any], forecasts: Mapping[str, Mapping[str, Any]],
    budget: float, allocated: Mapping[str, int], last_started: Mapping[str, int],
    keep: Iterable[str] = (),
    force: Iterable[str] = (),
    surplus_seconds: float = 0,
) -> dict[str, float]:
    """Allocate <=20 engines, bounded by profile, group count/rate and WAN budget."""
    group_map = {group.group_id: group for group in groups if group.enabled}
    accounts = list(accounts)
    forced = set(force)
    needs = {}
    for account in accounts:
        view = forecasts.get(account.account_id, {})
        debt = view.get("remaining_download_bytes", 0)
        tier = 0
        if debt <= 0 and surplus_seconds > 0:
            predicted = view.get("predicted_upload_bytes", 0)
            downloaded = view.get("today_download_bytes", 0)
            for tier, ratio in ((1, 2.5), (2, 3.0)):
                debt = max(0, int(predicted * ratio) - downloaded)
                if debt > 0:
                    break
            # Only attempt extra traffic that can fit before the upload window.
            if debt * 8 / (account.download_mbps * 1_000_000) > surplus_seconds:
                debt = 0
        needs[account.account_id] = (tier, debt)
    candidates = [account for account in accounts if account.group_id in group_map
                  and (account.account_id in forced or needs[account.account_id][1] > 0)]
    protected = set(keep)
    def priority(account: Any) -> tuple[Any, ...]:
        tier, debt = needs[account.account_id]
        seconds_needed = debt * 8 / (account.download_mbps * 1_000_000)
        return (account.account_id not in forced, tier, account.account_id not in protected, -seconds_needed,
                allocated.get(account.account_id, 0), last_started.get(account.account_id, 0), account.account_id)
    candidates.sort(key=priority)
    remaining = max(0.0, budget)
    group_rates: dict[str, float] = {}
    group_counts: dict[str, int] = {}
    result: dict[str, float] = {}
    for account in candidates:
        if len(result) >= 20 or remaining < 0.001:
            break
        group = group_map[account.group_id]
        if group_counts.get(group.group_id, 0) >= group.max_active:
            continue
        # Router enforces the whole-line profile. There is no application
        # limiter, so a fractional quota would not constrain actual traffic.
        rate = float(account.download_mbps)
        if rate > remaining + 0.001 or rate > group.download_limit_mbps - group_rates.get(group.group_id, 0.0) + 0.001:
            continue
        result[account.account_id] = rate
        remaining -= rate
        group_counts[group.group_id] = group_counts.get(group.group_id, 0) + 1
        group_rates[group.group_id] = group_rates.get(group.group_id, 0.0) + rate
    return result
