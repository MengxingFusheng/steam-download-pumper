from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import queue
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .config import FleetConfig, save_config
from .engine import EngineProcess
from .fleet_schedule import WanBudget, allocate_daily, daily_phase
from .remote_sources import RemoteSourceManager, RemoteSourceRefreshWorker, SourceListSnapshot
from .topology import LogicalLine, apply_ipv4_addresses
from .fleet_forecast import daily_prediction, evening_fallback, history_model
from .fleet_sources import FleetSources


SHANGHAI = ZoneInfo("Asia/Shanghai")
IF_NAME_OID = ".1.3.6.1.2.1.31.1.1.1.1"
IF_ALIAS_OID = ".1.3.6.1.2.1.31.1.1.1.18"
IF_HC_IN_OID = ".1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OID = ".1.3.6.1.2.1.31.1.1.1.10"
IF_DISCONTINUITY_OID = ".1.3.6.1.2.1.31.1.1.1.19"
IF_OPER_STATUS_OID = ".1.3.6.1.2.1.2.2.1.8"
SYS_UPTIME_OID = ".1.3.6.1.2.1.1.3.0"
MAX_ACCOUNTS = 256
CSV_FIELDS = {"account_id", "bind_ip", "snmp_interface", "enabled", "weight", "group_id"}
REQUIRED_CSV_FIELDS = {"account_id", "bind_ip", "snmp_interface"}


@dataclass(frozen=True)
class FleetAccount:
    account_id: str
    bind_ip: str
    snmp_interface: str
    enabled: bool = True
    weight: int | None = None
    group_id: str = ""

    @property
    def upload_mbps(self) -> int:
        return 500 if self.weight == 10 else 50

    @property
    def download_mbps(self) -> int:
        if self.weight == 10:
            return 500
        # Some 50 Mbps uplinks have a 500 Mbps downstream. Keep upload
        # forecasting independent of the downstream scheduling profile.
        try:
            rate = int(os.environ.get("FLEET_STANDARD_DOWNLOAD_MBPS", "200"))
        except ValueError as exc:
            raise ValueError("FLEET_STANDARD_DOWNLOAD_MBPS must be an integer") from exc
        if not 1 <= rate <= 1000:
            raise ValueError("FLEET_STANDARD_DOWNLOAD_MBPS must be between 1 and 1000")
        return rate

    @property
    def profile_confirmed(self) -> bool:
        return self.weight in {1, 10}


@dataclass(frozen=True)
class FleetGroup:
    group_id: str
    name: str
    download_limit_mbps: float
    max_active: int
    enabled: bool = True


@dataclass(frozen=True)
class SnmpInterface:
    index: int
    name: str
    alias: str
    in_octets: int
    out_octets: int
    discontinuity: int | None = None
    oper_status: int | None = None


class SnmpError(RuntimeError):
    pass


def _environment_flag(name: str, default: bool) -> bool:
    """Read an operational switch without making it mutable through the Web API."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


class SnmpV2Reader:
    """Small net-snmp boundary. A secret-only snmp.conf keeps community out of argv."""

    def __init__(
        self,
        host: str,
        community_file: str | Path,
        *,
        port: int = 161,
        timeout_seconds: int = 10,
    ) -> None:
        if not host:
            raise ValueError("SNMP_HOST is required")
        self.host = host
        self.port = port
        self.community_file = Path(community_file)
        self.timeout_seconds = timeout_seconds
        self._config_path: Path | None = None
        self.last_sys_uptime: int | None = None

    def read(self) -> list[SnmpInterface]:
        config = self._config_file()
        columns = {
            "name": self._walk(IF_NAME_OID, config),
            "alias": self._walk(IF_ALIAS_OID, config),
            "in": self._walk(IF_HC_IN_OID, config),
            "out": self._walk(IF_HC_OUT_OID, config),
            "disc": self._walk(IF_DISCONTINUITY_OID, config),
            "oper": self._walk(IF_OPER_STATUS_OID, config),
        }
        self.last_sys_uptime = self._get_int(SYS_UPTIME_OID, config)
        result: list[SnmpInterface] = []
        for index, name in columns["name"].items():
            try:
                in_octets = int(columns["in"][index])
                out_octets = int(columns["out"][index])
            except (KeyError, TypeError, ValueError):
                continue
            if in_octets < 0 or out_octets < 0:
                continue
            discontinuity: int | None = None
            try:
                discontinuity = int(columns["disc"].get(index, ""))
            except ValueError:
                pass
            oper_status: int | None = None
            try:
                oper_status = int(columns["oper"].get(index, ""))
            except ValueError:
                match = re.search(r"\((\d+)\)", columns["oper"].get(index, ""))
                oper_status = int(match.group(1)) if match else None
            result.append(SnmpInterface(
                index=index,
                name=_unquote(name),
                alias=_unquote(columns["alias"].get(index, "")),
                in_octets=in_octets,
                out_octets=out_octets,
                discontinuity=discontinuity,
                oper_status=oper_status,
            ))
        if not result:
            raise SnmpError("SNMP returned no usable interfaces")
        return result

    def _config_file(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        try:
            community = self.community_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SnmpError("SNMP community secret is unavailable") from exc
        if not community or "\n" in community or "\r" in community:
            raise SnmpError("SNMP community secret is invalid")
        runtime = Path("/run/pumper/snmp")
        runtime.mkdir(parents=True, exist_ok=True)
        self._config_path = runtime / "snmp.conf"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".snmp-", dir=runtime)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, f"defCommunity {community}\n".encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(temporary_name, self._config_path)
        return self._config_path

    def _walk(self, oid: str, config: Path) -> dict[int, str]:
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "SNMPCONFPATH": str(config.parent)}
        command = [
            "snmpbulkwalk", "-v2c", "-On", "-t", str(self.timeout_seconds), "-r", "1",
            f"{self.host}:{self.port}", oid,
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout_seconds + 3, check=False, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SnmpError("SNMP request failed") from exc
        if completed.returncode != 0:
            raise SnmpError("SNMP request failed")
        values: dict[int, str] = {}
        prefix = oid.lstrip(".") + "."
        for line in completed.stdout.splitlines():
            if " = " not in line:
                continue
            raw_oid, value = line.split(" = ", 1)
            normalized = raw_oid.lstrip(".")
            if not normalized.startswith(prefix):
                continue
            try:
                index = int(normalized.removeprefix(prefix))
            except ValueError:
                continue
            values[index] = _snmp_value(value)
        return values

    def _get_int(self, oid: str, config: Path) -> int | None:
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "SNMPCONFPATH": str(config.parent)}
        command = ["snmpget", "-v2c", "-On", "-t", str(self.timeout_seconds), "-r", "1", f"{self.host}:{self.port}", oid]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout_seconds + 3, check=False, env=environment)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or " = " not in completed.stdout:
            return None
        try:
            return int(_snmp_value(completed.stdout.split(" = ", 1)[1]))
        except ValueError:
            match = re.search(r"\((\d+)\)", completed.stdout)
            return int(match.group(1)) if match else None


def _snmp_value(value: str) -> str:
    if ":" not in value:
        return value.strip()
    return value.split(":", 1)[1].strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_accounts_csv(payload: str) -> list[FleetAccount]:
    if len(payload.encode("utf-8")) > 1_048_576:
        raise ValueError("account CSV is too large")
    reader = csv.DictReader(io.StringIO(payload))
    fields = set(reader.fieldnames or [])
    if not REQUIRED_CSV_FIELDS.issubset(fields) or not fields.issubset(CSV_FIELDS) or len(fields) != len(reader.fieldnames or []):
        raise ValueError("CSV must contain account_id,bind_ip,snmp_interface and optional enabled,weight,group_id")
    accounts: list[FleetAccount] = []
    ids: set[str] = set()
    ips: set[str] = set()
    interfaces: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"CSV row {row_number}: unexpected extra columns")
        account_id = (row.get("account_id") or "").strip()
        bind_ip = (row.get("bind_ip") or "").strip()
        interface = (row.get("snmp_interface") or "").strip()
        enabled_raw = (row.get("enabled") or "true").strip().lower()
        if not account_id or len(account_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", account_id):
            raise ValueError(f"CSV row {row_number}: invalid account_id")
        try:
            if ip_address(bind_ip).version != 4:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"CSV row {row_number}: bind_ip must be IPv4") from exc
        if not interface or len(interface) > 128 or "\x00" in interface:
            raise ValueError(f"CSV row {row_number}: invalid snmp_interface")
        if enabled_raw not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"CSV row {row_number}: invalid enabled")
        enabled = enabled_raw in {"1", "true", "yes", "on"}
        weight_raw = (row.get("weight") or "").strip()
        if weight_raw not in {"", "1", "10"}:
            raise ValueError(f"CSV row {row_number}: weight must be 1 or 10")
        weight = int(weight_raw) if weight_raw else None
        group_id = (row.get("group_id") or "").strip() or ("symmetric500" if weight == 10 else "standard")
        if len(group_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", group_id):
            raise ValueError(f"CSV row {row_number}: invalid group_id")
        if account_id in ids or bind_ip in ips or interface in interfaces:
            raise ValueError(f"CSV row {row_number}: account_id, bind_ip and snmp_interface must be unique")
        ids.add(account_id)
        ips.add(bind_ip)
        interfaces.add(interface)
        accounts.append(FleetAccount(account_id, bind_ip, interface, enabled, weight, group_id))
    if not accounts:
        raise ValueError("CSV must contain at least one account")
    if len(accounts) > MAX_ACCOUNTS:
        raise ValueError(f"CSV cannot contain more than {MAX_ACCOUNTS} accounts")
    return accounts


class FleetStore:
    def __init__(self, path: str | Path, cfg: FleetConfig) -> None:
        self.path = Path(path)
        self.cfg = cfg
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._history_cache_key: tuple[int, int] | None = None
        self._history_cache: dict[str, dict[int, float]] = {}
        self._history_fallback: dict[str, float] = {}
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
          account_id TEXT PRIMARY KEY, bind_ip TEXT NOT NULL UNIQUE,
          snmp_interface TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL,
          last_started INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS traffic (
          account_id TEXT NOT NULL, minute INTEGER NOT NULL,
          upload_bytes INTEGER NOT NULL DEFAULT 0, download_bytes INTEGER NOT NULL DEFAULT 0,
          snmp_download_bytes INTEGER NOT NULL DEFAULT 0,
          measured_upload_bytes INTEGER NOT NULL DEFAULT 0,
          measured_download_bytes INTEGER NOT NULL DEFAULT 0,
          estimated_upload_bytes INTEGER NOT NULL DEFAULT 0,
          estimated_download_bytes INTEGER NOT NULL DEFAULT 0,
          unknown_seconds INTEGER NOT NULL DEFAULT 0,
          sample_seconds INTEGER NOT NULL DEFAULT 0,
          interruptions INTEGER NOT NULL DEFAULT 0,
          quality TEXT NOT NULL DEFAULT 'ok',
          PRIMARY KEY (account_id, minute)
        );
        CREATE INDEX IF NOT EXISTS traffic_window ON traffic(minute, account_id);
        CREATE TABLE IF NOT EXISTS cursors (
          account_id TEXT PRIMARY KEY, if_index INTEGER, in_octets INTEGER,
          out_octets INTEGER, discontinuity INTEGER, sampled_at INTEGER NOT NULL,
          valid INTEGER NOT NULL DEFAULT 0, baseline_at INTEGER,
          device_uptime INTEGER, missing_since INTEGER, oper_status INTEGER
        );
        CREATE TABLE IF NOT EXISTS runtime_minutes (
          account_id TEXT NOT NULL, minute INTEGER NOT NULL, allocated_seconds INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (account_id, minute)
        );
        CREATE INDEX IF NOT EXISTS runtime_window ON runtime_minutes(minute, account_id);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fleet_groups (
          group_id TEXT PRIMARY KEY,name TEXT NOT NULL,
          download_limit_mbps REAL NOT NULL,max_active INTEGER NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1
        );
        """)
        self._ensure_columns("accounts", {
            "weight": "INTEGER",
            "group_id": "TEXT NOT NULL DEFAULT 'standard'",
        })
        self._ensure_columns("traffic", {
            "measured_upload_bytes": "INTEGER NOT NULL DEFAULT 0",
            "measured_download_bytes": "INTEGER NOT NULL DEFAULT 0",
            "estimated_upload_bytes": "INTEGER NOT NULL DEFAULT 0",
            "estimated_download_bytes": "INTEGER NOT NULL DEFAULT 0",
            "unknown_seconds": "INTEGER NOT NULL DEFAULT 0",
            "sample_seconds": "INTEGER NOT NULL DEFAULT 0",
            "interruptions": "INTEGER NOT NULL DEFAULT 0",
            "upload_valid_seconds": "INTEGER NOT NULL DEFAULT 0",
            "upload_capacity_bytes": "REAL NOT NULL DEFAULT 0",
            "unknown_upload_seconds": "INTEGER NOT NULL DEFAULT 0",
            "unknown_upload_capacity_bytes": "REAL NOT NULL DEFAULT 0",
        })
        self._ensure_columns("cursors", {
            "baseline_at": "INTEGER",
            "device_uptime": "INTEGER",
            "missing_since": "INTEGER",
            "oper_status": "INTEGER",
        })
        self.db.execute("UPDATE cursors SET baseline_at=sampled_at WHERE baseline_at IS NULL AND valid=1")
        if self._meta("snmp_actual_schema_at") is None:
            self._set_meta("snmp_actual_schema_at", str(int(time.time())))
        # Before profile-aware accounting every accepted upload was checked
        # against the old 50 Mbps ceiling. Preserve those valid samples once.
        if self._meta("daily_profile_schema_at") is None:
            self._migrate_daily_profiles()
        self.db.executemany("""
        INSERT OR IGNORE INTO fleet_groups(group_id,name,download_limit_mbps,max_active,enabled)
        VALUES (?,?,?,?,1)
        """, [("standard", "普通线路", 4000, 20), ("symmetric500", "500M 对等线路", 5000, 10)])
        self.db.commit()

    def _migrate_daily_profiles(self) -> None:
        """Backfill derived history without overflowing the small /tmp tmpfs.

        An UPDATE of millions of rows inside an existing write transaction can
        create a large SQLite statement journal. Commit bounded hourly chunks;
        all assignments are idempotent, so an interrupted migration can resume.
        Actual measured/estimated counters are never changed by this migration.
        """
        self.db.commit()
        bounds = self.db.execute("SELECT MIN(minute),MAX(minute) FROM traffic").fetchone()
        if bounds[0] is not None:
            for lower in range(int(bounds[0]), int(bounds[1]) + 1, 60):
                with self.db:
                    self.db.execute("""
                    UPDATE traffic SET upload_valid_seconds=sample_seconds,
                      upload_capacity_bytes=sample_seconds*6250000.0
                    WHERE minute>=? AND minute<? AND sample_seconds>0
                      AND quality IN ('ok','recovered') AND upload_capacity_bytes=0
                    """, (lower, lower + 60))
                    self.db.execute("""
                    UPDATE traffic SET unknown_upload_seconds=unknown_seconds,
                      unknown_upload_capacity_bytes=unknown_seconds*6250000.0
                    WHERE minute>=? AND minute<? AND unknown_seconds>0
                      AND unknown_upload_capacity_bytes=0
                    """, (lower, lower + 60))
        with self.db:
            self._set_meta("daily_profile_schema_at", str(int(time.time())))

    def _ensure_columns(self, table: str, columns: Mapping[str, str]) -> None:
        existing = {str(row["name"]) for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self.db.close()

    def accounts(self) -> list[FleetAccount]:
        rows = self.db.execute("SELECT account_id,bind_ip,snmp_interface,enabled,weight,group_id FROM accounts ORDER BY account_id").fetchall()
        return [FleetAccount(row["account_id"], row["bind_ip"], row["snmp_interface"], bool(row["enabled"]), row["weight"], row["group_id"]) for row in rows]

    def validate_accounts(self, accounts: list[FleetAccount]) -> None:
        known_groups = {group.group_id for group in self.groups()}
        for item in accounts:
            group_id = item.group_id or ("symmetric500" if item.weight == 10 else "standard")
            if group_id not in known_groups:
                raise ValueError(f"unknown group_id: {group_id}")
            if item.weight not in {None, 1, 10}:
                raise ValueError(f"invalid weight for account {item.account_id}")

    def validate_daily_ready(self, accounts: list[FleetAccount] | None = None) -> None:
        selected = self.accounts() if accounts is None else accounts
        self.validate_accounts(selected)
        unconfirmed = [item.account_id for item in selected if item.enabled and not item.profile_confirmed]
        if unconfirmed:
            raise ValueError("daily mode requires a weight-confirmed CSV for all enabled accounts: " + ",".join(unconfirmed[:5]))

    def import_accounts(self, accounts: list[FleetAccount]) -> None:
        self.validate_accounts(accounts)
        prior = {row["account_id"]: row for row in self.db.execute("SELECT account_id,snmp_interface,last_started FROM accounts")}
        with self.db:
            self.db.execute("DELETE FROM accounts")
            self.db.executemany(
                "INSERT INTO accounts(account_id,bind_ip,snmp_interface,enabled,weight,group_id,last_started) VALUES (?,?,?,?,?,?,?)",
                [(item.account_id, item.bind_ip, item.snmp_interface, int(item.enabled), item.weight,
                  item.group_id or ("symmetric500" if item.weight == 10 else "standard"),
                  prior[item.account_id]["last_started"] if item.account_id in prior else 0) for item in accounts],
            )
            for item in accounts:
                if item.account_id in prior and prior[item.account_id]["snmp_interface"] != item.snmp_interface:
                    self.db.execute("DELETE FROM cursors WHERE account_id=?", (item.account_id,))
        self._history_cache_key = None

    def groups(self) -> list[FleetGroup]:
        return [FleetGroup(str(row["group_id"]), str(row["name"]), float(row["download_limit_mbps"]), int(row["max_active"]), bool(row["enabled"]))
                for row in self.db.execute("SELECT * FROM fleet_groups ORDER BY group_id")]

    def update_groups(self, groups: list[dict[str, Any]]) -> None:
        if not isinstance(groups, list) or not groups or len(groups) > MAX_ACCOUNTS:
            raise ValueError("groups must be a non-empty list")
        validated: list[FleetGroup] = []
        seen: set[str] = set()
        fields = {"group_id", "name", "download_limit_mbps", "max_active", "enabled"}
        for payload in groups:
            if not isinstance(payload, dict) or set(payload) - fields:
                raise ValueError("invalid group fields")
            group_id = payload.get("group_id", "")
            name = payload.get("name", "")
            limit = payload.get("download_limit_mbps")
            count = payload.get("max_active")
            enabled = payload.get("enabled", True)
            if not isinstance(group_id, str) or len(group_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", group_id) or group_id in seen:
                raise ValueError("group_id must be valid and unique")
            if not isinstance(name, str) or not name.strip() or len(name) > 128 or "\x00" in name:
                raise ValueError("invalid group name")
            if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or not 0 < limit <= 10000:
                raise ValueError("download_limit_mbps must be between 0 and 10000")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
                raise ValueError("max_active must be an integer between 1 and 20")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be boolean")
            seen.add(group_id)
            validated.append(FleetGroup(group_id, name.strip(), float(limit), count, enabled))
        with self.db:
            self.db.executemany("""
            INSERT INTO fleet_groups(group_id,name,download_limit_mbps,max_active,enabled) VALUES (?,?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET name=excluded.name,download_limit_mbps=excluded.download_limit_mbps,
              max_active=excluded.max_active,enabled=excluded.enabled
            """, [(g.group_id, g.name, g.download_limit_mbps, g.max_active, int(g.enabled)) for g in validated])

    def assign_group(self, group_id: str, account_ids: list[str]) -> None:
        if not isinstance(group_id, str) or group_id not in {group.group_id for group in self.groups()}:
            raise ValueError("unknown group_id")
        if not isinstance(account_ids, list) or not account_ids or not all(isinstance(item, str) for item in account_ids) or len(set(account_ids)) != len(account_ids):
            raise ValueError("account_ids must be a non-empty list of unique account IDs")
        known = {account.account_id for account in self.accounts()}
        if set(account_ids) - known:
            raise ValueError("unknown account_id")
        with self.db:
            self.db.executemany("UPDATE accounts SET group_id=? WHERE account_id=?", [(group_id, account_id) for account_id in account_ids])

    def _add_traffic(
        self,
        account_id: str,
        minute: int,
        *,
        upload: int = 0,
        download: int = 0,
        snmp_download: int = 0,
        measured_upload: int = 0,
        measured_download: int = 0,
        estimated_upload: int = 0,
        estimated_download: int = 0,
        unknown_seconds: int = 0,
        sample_seconds: int = 0,
        interruptions: int = 0,
        upload_valid_seconds: int = 0,
        upload_capacity_bytes: int = 0,
        unknown_upload_seconds: int = 0,
        unknown_upload_capacity_bytes: int = 0,
        quality: str = "ok",
    ) -> None:
        self.db.execute("""
        INSERT INTO traffic(
          account_id,minute,upload_bytes,download_bytes,snmp_download_bytes,
          measured_upload_bytes,measured_download_bytes,estimated_upload_bytes,estimated_download_bytes,
          unknown_seconds,sample_seconds,interruptions,quality,
          upload_valid_seconds,upload_capacity_bytes,unknown_upload_seconds,unknown_upload_capacity_bytes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id,minute) DO UPDATE SET
          upload_bytes=upload_bytes+excluded.upload_bytes,
          download_bytes=download_bytes+excluded.download_bytes,
          snmp_download_bytes=snmp_download_bytes+excluded.snmp_download_bytes,
          measured_upload_bytes=measured_upload_bytes+excluded.measured_upload_bytes,
          measured_download_bytes=measured_download_bytes+excluded.measured_download_bytes,
          estimated_upload_bytes=estimated_upload_bytes+excluded.estimated_upload_bytes,
          estimated_download_bytes=estimated_download_bytes+excluded.estimated_download_bytes,
          unknown_seconds=unknown_seconds+excluded.unknown_seconds,
          sample_seconds=sample_seconds+excluded.sample_seconds,
          interruptions=interruptions+excluded.interruptions,
          upload_valid_seconds=upload_valid_seconds+excluded.upload_valid_seconds,
          upload_capacity_bytes=upload_capacity_bytes+excluded.upload_capacity_bytes,
          unknown_upload_seconds=unknown_upload_seconds+excluded.unknown_upload_seconds,
          unknown_upload_capacity_bytes=unknown_upload_capacity_bytes+excluded.unknown_upload_capacity_bytes,
          quality=CASE WHEN traffic.quality='ok' THEN excluded.quality ELSE traffic.quality END
        """, (
            account_id, minute, max(0, upload), max(0, download), max(0, snmp_download),
            max(0, measured_upload), max(0, measured_download), max(0, estimated_upload),
            max(0, estimated_download), max(0, unknown_seconds), max(0, sample_seconds),
            max(0, interruptions), quality,
            max(0, upload_valid_seconds), max(0, upload_capacity_bytes),
            max(0, unknown_upload_seconds), max(0, unknown_upload_capacity_bytes),
        ))

    def record_download(self, account_id: str, amount: int, observed_at: datetime) -> None:
        if amount <= 0:
            return
        minute = int(observed_at.timestamp() // 60)
        with self.db:
            self._add_traffic(account_id, minute, download=amount)

    def record_snmp(
        self,
        interfaces: Iterable[SnmpInterface],
        observed_at: datetime,
        *,
        device_uptime: int | None = None,
    ) -> dict[str, str]:
        now = observed_at.astimezone(timezone.utc)
        by_identity: dict[str, list[SnmpInterface]] = {}
        for item in interfaces:
            for identity in {item.name, item.alias} - {""}:
                by_identity.setdefault(identity, []).append(item)
        results: dict[str, str] = {}
        with self.db:
            if self._meta("baseline_at") is None:
                self._set_meta("baseline_at", str(int(now.timestamp())))
            for account in self.accounts():
                matches = by_identity.get(account.snmp_interface, [])
                # iKuai can retain several down interfaces with the same name
                # after PPPoE reconnects. A single live instance is the
                # authoritative successor regardless of stale down entries.
                live_matches = [item for item in matches if item.oper_status in {None, 1}]
                observed = live_matches[0] if len(live_matches) == 1 else None
                unavailable_state = "ambiguous" if len(live_matches) > 1 else ("offline" if matches else "missing")
                prior = self.db.execute("SELECT * FROM cursors WHERE account_id=?", (account.account_id,)).fetchone()
                if prior is None:
                    if observed is not None:
                        self._save_cursor(account.account_id, observed, now, observed.oper_status in {None, 1}, device_uptime)
                        results[account.account_id] = "baseline" if observed.oper_status in {None, 1} else "offline"
                    else:
                        self._save_missing_cursor(account.account_id, now)
                        results[account.account_id] = unavailable_state
                    continue

                previous_poll = datetime.fromtimestamp(prior["sampled_at"], timezone.utc)
                baseline_at = datetime.fromtimestamp(prior["baseline_at"] or prior["sampled_at"], timezone.utc)
                if observed is None:
                    self._add_interval_traffic(
                        account.account_id, previous_poll, now,
                        unknown_seconds=int(max(0, (now - previous_poll).total_seconds())),
                        unknown_upload_seconds=int(max(0, (now - previous_poll).total_seconds())),
                        unknown_upload_capacity_bytes=int(max(0, (now - previous_poll).total_seconds()) * account.upload_mbps * 1_000_000 / 8),
                        quality="pending_gap",
                    )
                    self._save_missing_cursor(account.account_id, now)
                    results[account.account_id] = unavailable_state
                    continue

                if observed.oper_status not in {None, 1}:
                    self._save_cursor(account.account_id, observed, now, False, device_uptime)
                    results[account.account_id] = "offline"
                    continue

                device_reset = (
                    prior["device_uptime"] is not None
                    and device_uptime is not None
                    and device_uptime < prior["device_uptime"]
                )
                counter_reset = (
                    prior["if_index"] is None
                    or prior["if_index"] != observed.index
                    or prior["in_octets"] is None
                    or prior["out_octets"] is None
                    or observed.in_octets < prior["in_octets"]
                    or observed.out_octets < prior["out_octets"]
                    or (
                        prior["discontinuity"] is not None
                        and observed.discontinuity is not None
                        and prior["discontinuity"] != observed.discontinuity
                    )
                )
                if device_reset or counter_reset:
                    self._clear_pending_unknown(account.account_id, baseline_at, now)
                    self._record_estimated_gap(account.account_id, baseline_at, now, quality="reset")
                    self._save_cursor(account.account_id, observed, now, True, device_uptime)
                    results[account.account_id] = "reset"
                    continue

                self._clear_pending_unknown(account.account_id, baseline_at, now)
                elapsed = max(0.0, (now - baseline_at).total_seconds())
                if self.cfg.snmp_direction_swap:
                    upload = observed.in_octets - prior["in_octets"]
                    snmp_download = observed.out_octets - prior["out_octets"]
                else:
                    upload = observed.out_octets - prior["out_octets"]
                    snmp_download = observed.in_octets - prior["in_octets"]
                upload_ceiling = account.upload_mbps if account.profile_confirmed else self.cfg.upload_ceiling_mbps
                # Rated capacity drives forecasts; the configurable physical
                # ceiling can admit legitimate ISP overdelivery without losing
                # the authoritative SNMP bytes or changing the account profile.
                upload_physical_ceiling = max(upload_ceiling, self.cfg.upload_ceiling_mbps)
                upload_maximum = int(upload_physical_ceiling * 1_000_000 / 8 * elapsed * 1.05)
                download_maximum = int(self.cfg.download_ceiling_mbps * 1_000_000 / 8 * elapsed * 1.05)
                upload_valid = 0 <= upload <= upload_maximum
                download_valid = 0 <= snmp_download <= download_maximum
                estimated_upload = 0 if upload_valid else self._estimated_bytes(account.account_id, baseline_at, now, "upload")
                estimated_download = 0 if download_valid else self._estimated_bytes(account.account_id, baseline_at, now, "download")
                unknown = int(elapsed) if (not upload_valid and estimated_upload is None) or (not download_valid and estimated_download is None) else 0
                unknown_upload = int(elapsed) if not upload_valid and estimated_upload is None else 0
                quality = "recovered" if not prior["valid"] else ("ok" if upload_valid and download_valid else "anomaly")
                self._add_interval_traffic(
                    account.account_id, baseline_at, now,
                    measured_upload=upload if upload_valid else 0,
                    measured_download=snmp_download if download_valid else 0,
                    estimated_upload=estimated_upload or 0,
                    estimated_download=estimated_download or 0,
                    unknown_seconds=unknown,
                    sample_seconds=int(elapsed),
                    upload_valid_seconds=int(elapsed) if upload_valid else 0,
                    upload_capacity_bytes=int(upload_ceiling * 1_000_000 / 8 * elapsed) if upload_valid else 0,
                    unknown_upload_seconds=unknown_upload,
                    unknown_upload_capacity_bytes=int(upload_ceiling * 1_000_000 / 8 * unknown_upload),
                    quality=quality,
                )
                self._save_cursor(account.account_id, observed, now, True, device_uptime)
                results[account.account_id] = quality
            self._prune(now)
        return results

    def _save_cursor(
        self,
        account_id: str,
        item: SnmpInterface,
        now: datetime,
        valid: bool,
        device_uptime: int | None,
    ) -> None:
        timestamp = int(now.timestamp())
        self.db.execute("""
        INSERT INTO cursors(
          account_id,if_index,in_octets,out_octets,discontinuity,sampled_at,valid,
          baseline_at,device_uptime,missing_since,oper_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET if_index=excluded.if_index,in_octets=excluded.in_octets,
          out_octets=excluded.out_octets,discontinuity=excluded.discontinuity,sampled_at=excluded.sampled_at,
          valid=excluded.valid,baseline_at=excluded.baseline_at,device_uptime=excluded.device_uptime,
          missing_since=NULL,oper_status=excluded.oper_status
        """, (
            account_id, item.index, item.in_octets, item.out_octets, item.discontinuity,
            timestamp, int(valid), timestamp, device_uptime, None, item.oper_status,
        ))

    def _save_missing_cursor(self, account_id: str, now: datetime) -> None:
        timestamp = int(now.timestamp())
        self.db.execute("""
        INSERT INTO cursors(account_id,sampled_at,valid,missing_since) VALUES (?,?,0,?)
        ON CONFLICT(account_id) DO UPDATE SET sampled_at=excluded.sampled_at,valid=0,
          missing_since=COALESCE(cursors.missing_since,excluded.missing_since)
        """, (account_id, timestamp, timestamp))

    def _clear_pending_unknown(self, account_id: str, start: datetime, end: datetime) -> None:
        first = int(start.timestamp() // 60)
        last = int(end.timestamp() // 60)
        self.db.execute("""
        UPDATE traffic SET unknown_seconds=0,unknown_upload_seconds=0,unknown_upload_capacity_bytes=0,
          quality=CASE WHEN quality='pending_gap' THEN 'recovered' ELSE quality END
        WHERE account_id=? AND minute BETWEEN ? AND ?
        """, (account_id, first, last))

    def _record_estimated_gap(self, account_id: str, start: datetime, end: datetime, *, quality: str) -> None:
        elapsed = int(max(0, (end - start).total_seconds()))
        estimated_upload = self._estimated_bytes(account_id, start, end, "upload")
        estimated_download = self._estimated_bytes(account_id, start, end, "download")
        capped_seconds = min(elapsed, 30 * 60)
        unknown = max(0, elapsed - capped_seconds)
        if estimated_upload is None or estimated_download is None:
            unknown = elapsed
        unknown_upload = elapsed if estimated_upload is None else max(0, elapsed - capped_seconds)
        account = next((item for item in self.accounts() if item.account_id == account_id), None)
        upload_ceiling = account.upload_mbps if account and account.profile_confirmed else self.cfg.upload_ceiling_mbps
        capped_end = min(end, start + timedelta(minutes=30))
        self._add_interval_traffic(
            account_id, start, capped_end,
            estimated_upload=estimated_upload or 0,
            estimated_download=estimated_download or 0,
            sample_seconds=capped_seconds,
            interruptions=1,
            quality=quality,
        )
        self._add_interval_traffic(account_id, start, end, unknown_seconds=unknown, quality=quality)
        unknown_start = start if estimated_upload is None else capped_end
        self._add_interval_traffic(
            account_id, unknown_start, end, unknown_upload_seconds=unknown_upload,
            unknown_upload_capacity_bytes=int(upload_ceiling * 1_000_000 / 8 * unknown_upload), quality=quality,
        )

    def _estimated_bytes(self, account_id: str, start: datetime, end: datetime, direction: str) -> int | None:
        rate = self._recent_measured_rate(account_id, start, direction, minutes=30)
        if rate is None:
            return None
        if direction == "upload":
            row = self.db.execute("SELECT weight FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            ceiling = 500 if row and row["weight"] == 10 else (50 if row and row["weight"] == 1 else self.cfg.upload_ceiling_mbps)
            rate = min(rate, ceiling * 1_000_000 / 8)
        capped_end = min(end, start + timedelta(minutes=30))
        seconds = self._business_seconds(start, capped_end) if direction == "upload" else max(0.0, (capped_end - start).total_seconds())
        return int(rate * seconds)

    def _recent_measured_rate(self, account_id: str, before: datetime, direction: str, *, minutes: int) -> float | None:
        column = "measured_upload_bytes" if direction == "upload" else "measured_download_bytes"
        upper = int(before.timestamp() // 60)
        lower = int((before - timedelta(minutes=minutes)).timestamp() // 60)
        row = self.db.execute(f"""
        SELECT COALESCE(SUM({column}),0) AS bytes,COALESCE(SUM(sample_seconds),0) AS seconds
        FROM traffic WHERE account_id=? AND minute>=? AND minute<=?
        """, (account_id, lower, upper)).fetchone()
        return float(row["bytes"]) / float(row["seconds"]) if row and row["seconds"] > 0 else None

    def _add_interval_traffic(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        **values: int | str,
    ) -> None:
        elapsed = max(0.0, (end - start).total_seconds())
        if elapsed <= 0:
            return
        segments: list[tuple[int, float]] = []
        cursor = start
        while cursor < end:
            minute = int(cursor.timestamp() // 60)
            boundary = datetime.fromtimestamp((minute + 1) * 60, timezone.utc)
            segment_end = min(end, boundary)
            segments.append((minute, (segment_end - cursor).total_seconds()))
            cursor = segment_end
        numeric_names = [name for name in values if name != "quality"]
        remaining = {name: int(values[name]) for name in numeric_names}
        for index, (minute, seconds) in enumerate(segments):
            allocated: dict[str, int] = {}
            for name in numeric_names:
                amount = remaining[name] if index == len(segments) - 1 else int(int(values[name]) * seconds / elapsed)
                allocated[name] = amount
                remaining[name] -= amount
            self._add_traffic(account_id, minute, quality=str(values.get("quality", "ok")), **allocated)

    @staticmethod
    def _business_seconds(start: datetime, end: datetime) -> float:
        if end <= start:
            return 0.0
        cursor = start.astimezone(SHANGHAI)
        finish = end.astimezone(SHANGHAI)
        seconds = 0.0
        while cursor.date() <= finish.date():
            window_start = datetime.combine(cursor.date(), datetime.min.time(), SHANGHAI).replace(hour=18)
            window_end = window_start + timedelta(hours=6)
            left = max(cursor, window_start)
            right = min(finish, window_end)
            if right > left:
                seconds += (right - left).total_seconds()
            cursor = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time(), SHANGHAI)
        return seconds

    def rolling(self, now: datetime) -> dict[str, dict[str, int]]:
        lower = int((now.astimezone(timezone.utc) - timedelta(days=self.cfg.rolling_days)).timestamp() // 60)
        return self._totals_since(lower)

    def today(self, now: datetime) -> dict[str, dict[str, int]]:
        """Return traffic booked since local midnight in Asia/Shanghai."""
        return self.daily_totals(now.astimezone(SHANGHAI).date())

    def daily_totals(self, day: date, before_hour: int | None = None) -> dict[str, dict[str, int]]:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError("day must be a date")
        if before_hour is not None and (isinstance(before_hour, bool) or not isinstance(before_hour, int) or not 0 <= before_hour <= 24):
            raise ValueError("before_hour must be an integer between 0 and 24")
        start = datetime.combine(day, datetime.min.time(), SHANGHAI)
        finish = start + timedelta(hours=24 if before_hour is None else before_hour)
        return self._totals_between(int(start.timestamp() // 60), int(finish.timestamp() // 60))

    def _totals_since(self, lower: int) -> dict[str, dict[str, int]]:
        return self._totals_between(lower)

    def _totals_between(self, lower: int, upper: int | None = None) -> dict[str, dict[str, int]]:
        condition = "minute>=?" if upper is None else "minute>=? AND minute<?"
        parameters = (lower,) if upper is None else (lower, upper)
        rows = self.db.execute(f"""
        SELECT account_id,
          COALESCE(SUM(upload_bytes),0) AS legacy_upload,
          COALESCE(SUM(snmp_download_bytes),0) AS legacy_download,
          COALESCE(SUM(download_bytes),0) AS program_download,
          COALESCE(SUM(measured_upload_bytes),0) AS measured_upload,
          COALESCE(SUM(measured_download_bytes),0) AS measured_download,
          COALESCE(SUM(estimated_upload_bytes),0) AS estimated_upload,
          COALESCE(SUM(estimated_download_bytes),0) AS estimated_download,
          COALESCE(SUM(unknown_seconds),0) AS unknown_seconds,
          COALESCE(SUM(unknown_upload_seconds),0) AS unknown_upload_seconds,
          COALESCE(SUM(unknown_upload_capacity_bytes),0) AS unknown_upload_capacity,
          COALESCE(SUM(interruptions),0) AS interruptions
        FROM traffic WHERE {condition} GROUP BY account_id
        """, parameters).fetchall()
        values: dict[str, dict[str, int]] = {}
        for row in rows:
            item = {name: int(row[name]) for name in (
                "legacy_upload", "legacy_download", "program_download",
                "measured_upload", "measured_download", "estimated_upload",
                "estimated_download", "unknown_seconds", "unknown_upload_seconds", "unknown_upload_capacity", "interruptions",
            )}
            item["upload"] = item["legacy_upload"] + item["measured_upload"] + item["estimated_upload"]
            item["download"] = item["legacy_download"] + item["measured_download"] + item["estimated_download"]
            item["snmp_download"] = item["download"]
            values[str(row["account_id"])] = item
        empty = {
            "legacy_upload": 0, "legacy_download": 0, "program_download": 0,
            "measured_upload": 0, "measured_download": 0,
            "estimated_upload": 0, "estimated_download": 0,
            "unknown_seconds": 0, "unknown_upload_seconds": 0, "unknown_upload_capacity": 0, "interruptions": 0,
            "upload": 0, "download": 0, "snmp_download": 0,
        }
        for account in self.accounts():
            values.setdefault(account.account_id, dict(empty))
        return values

    def _historical_utilization(self, now: datetime) -> dict[str, dict[int, float]]:
        local_now = now.astimezone(SHANGHAI)
        key = (local_now.date().toordinal(), local_now.hour)
        if self._history_cache_key == key:
            return self._history_cache
        end = datetime.combine(local_now.date(), datetime.min.time(), SHANGHAI)
        lower = int((end - timedelta(days=7)).timestamp() // 60)
        upper = int(local_now.timestamp() // 60)
        rows = self.db.execute("""
        SELECT account_id,CAST((minute-?)/1440 AS INTEGER) AS historical_day,
          CAST(((minute-?)%1440)/15 AS INTEGER) AS interval,
          SUM(measured_upload_bytes) AS upload,SUM(upload_capacity_bytes) AS capacity,
          SUM(upload_valid_seconds) AS valid_seconds
        FROM traffic WHERE minute>=? AND minute<?
        GROUP BY account_id,historical_day,interval
        """, (lower, lower, lower, upper)).fetchall()
        self._history_cache = history_model([row for row in rows if row["historical_day"] < 7])
        self._history_fallback = evening_fallback(rows)
        self._history_cache_key = key
        return self._history_cache

    def daily_forecasts(self, now: datetime) -> dict[str, dict[str, Any]]:
        local_now = now.astimezone(SHANGHAI)
        totals = self.daily_totals(local_now.date())
        historical = self._historical_utilization(local_now)
        result: dict[str, dict[str, Any]] = {}
        for account in self.accounts():
            values = daily_prediction(
                local_now, upload_mbps=account.upload_mbps,
                totals=totals[account.account_id], historical_utilization=historical.get(account.account_id, {}),
                target_ratio=self.cfg.target_ratio,
                physical_upload_mbps=self.cfg.upload_ceiling_mbps,
                fallback_utilization=self._history_fallback.get(account.account_id, 1.0),
            )
            # Unprefixed measured fields are convenient for API aggregation.
            values.update({
                "measured_upload_bytes": totals[account.account_id]["measured_upload"],
                "measured_download_bytes": totals[account.account_id]["measured_download"],
                "estimated_upload_bytes": totals[account.account_id]["estimated_upload"],
                "estimated_download_bytes": totals[account.account_id]["estimated_download"],
                "unknown_seconds": totals[account.account_id]["unknown_seconds"],
            })
            result[account.account_id] = values
        return result

    def daily_report(self, day: date) -> dict[str, dict[str, Any]]:
        # A completed-day report uses its final ledger, not today's forecast or
        # any debt from the previous day. One microsecond keeps the correct date.
        finish = datetime.combine(day + timedelta(days=1), datetime.min.time(), SHANGHAI)
        now = min(datetime.now(SHANGHAI), finish - timedelta(microseconds=1))
        if now.date() != day:
            raise ValueError("daily reports cannot use a future date")
        forecasts = self.daily_forecasts(now)
        before18 = self.daily_totals(day, before_hour=18)
        pre18_targets = json.loads(self._meta(f"daily_pre18:{day.isoformat()}") or "{}")
        result: dict[str, dict[str, Any]] = {}
        for account in self.accounts():
            values = dict(forecasts[account.account_id])
            if datetime.now(SHANGHAI) >= finish:
                values["future_upload_bytes"] = 0
                values["predicted_upload_bytes"] = values["today_upload_bytes"] + values["unknown_upload_budget_bytes"]
                values["target_download_bytes"] = int(values["predicted_upload_bytes"] * self.cfg.target_ratio)
                values["remaining_download_bytes"] = max(0, values["target_download_bytes"] - values["today_download_bytes"])
                target_bytes = values["target_download_bytes"]
                values["target_progress_percent"] = min(100.0, values["today_download_bytes"] * 100.0 / target_bytes) if target_bytes else 100.0
            early = before18[account.account_id]
            target = pre18_targets.get(account.account_id)
            values.update({
                "date": day.isoformat(),
                "before18_upload_bytes": early["upload"],
                "before18_download_bytes": early["download"],
                "before18_target_download_bytes": target,
                "before18_achieved": early["download"] >= target if isinstance(target, (int, float)) else None,
                "final_achieved": values["today_download_bytes"] >= values["target_download_bytes"],
            })
            result[account.account_id] = values
        return result

    def recent_upload_rates(self, now: datetime) -> dict[str, float]:
        rates: dict[str, float] = {}
        for account in self.accounts():
            value = self._recent_measured_rate(account.account_id, now.astimezone(timezone.utc), "upload", minutes=30)
            if value is None:
                value = self._recent_measured_rate(account.account_id, now.astimezone(timezone.utc), "upload", minutes=24 * 60)
            rates[account.account_id] = value or 0.0
        return rates

    def record_runtime(self, account_id: str, start: datetime, end: datetime) -> None:
        seconds = int(max(0, (end - start).total_seconds()))
        if seconds <= 0:
            return
        with self.db:
            cursor = start
            remaining = seconds
            while cursor < end:
                minute = int(cursor.timestamp() // 60)
                boundary = datetime.fromtimestamp((minute + 1) * 60, timezone.utc)
                segment_end = min(end, boundary)
                amount = remaining if segment_end == end else min(remaining, int((segment_end - cursor).total_seconds()))
                self.db.execute("""
                INSERT INTO runtime_minutes(account_id,minute,allocated_seconds) VALUES (?,?,?)
                ON CONFLICT(account_id,minute) DO UPDATE SET allocated_seconds=allocated_seconds+excluded.allocated_seconds
                """, (account_id, minute, amount))
                remaining -= amount
                cursor = segment_end

    def allocated_last24(self, now: datetime) -> dict[str, int]:
        lower = int((now - timedelta(hours=24)).timestamp() // 60)
        rows = self.db.execute("""
        SELECT account_id,COALESCE(SUM(allocated_seconds),0) AS seconds
        FROM runtime_minutes WHERE minute>=? GROUP BY account_id
        """, (lower,)).fetchall()
        return {str(row["account_id"]): int(row["seconds"]) for row in rows}

    def allocated_today(self, now: datetime) -> dict[str, int]:
        local_now = now.astimezone(SHANGHAI)
        start = datetime.combine(local_now.date(), datetime.min.time(), SHANGHAI)
        finish = start + timedelta(days=1)
        rows = self.db.execute("""
        SELECT account_id,COALESCE(SUM(allocated_seconds),0) AS seconds
        FROM runtime_minutes WHERE minute>=? AND minute<? GROUP BY account_id
        """, (int(start.timestamp() // 60), int(finish.timestamp() // 60))).fetchall()
        return {str(row["account_id"]): int(row["seconds"]) for row in rows}

    def cursor_status(self) -> dict[str, dict[str, int | None]]:
        rows = self.db.execute("""
        SELECT account_id,sampled_at,baseline_at,missing_since,oper_status FROM cursors
        """).fetchall()
        return {
            str(row["account_id"]): {
                "sampled_at": int(row["sampled_at"]),
                "baseline_at": int(row["baseline_at"]) if row["baseline_at"] is not None else None,
                "missing_since": int(row["missing_since"]) if row["missing_since"] is not None else None,
                "oper_status": int(row["oper_status"]) if row["oper_status"] is not None else None,
            }
            for row in rows
        }

    def warmup(self, now: datetime) -> dict[str, Any]:
        baseline = self._meta("baseline_at")
        if baseline is None:
            return {"complete": False, "remaining_seconds": self.cfg.rolling_days * 86400}
        elapsed = max(0, int(now.timestamp()) - int(baseline))
        return {"complete": elapsed >= self.cfg.rolling_days * 86400, "remaining_seconds": max(0, self.cfg.rolling_days * 86400 - elapsed)}

    def mark_started(self, account_id: str, at: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE accounts SET last_started=? WHERE account_id=?", (int(at.timestamp()), account_id))

    def last_started(self) -> dict[str, int]:
        return {row["account_id"]: int(row["last_started"]) for row in self.db.execute("SELECT account_id,last_started FROM accounts")}

    def _prune(self, now: datetime) -> None:
        lower = int((now - timedelta(days=max(16, self.cfg.rolling_days + 1))).timestamp() // 60)
        self.db.execute("DELETE FROM traffic WHERE minute<?", (lower,))
        runtime_lower = int((now - timedelta(days=2)).timestamp() // 60)
        self.db.execute("DELETE FROM runtime_minutes WHERE minute<?", (runtime_lower,))

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def load_manual_enabled(self, default: bool) -> bool:
        value = self._meta("manual_enabled")
        return default if value is None else value == "1"

    def save_manual_enabled(self, enabled: bool) -> None:
        with self.db:
            self._set_meta("manual_enabled", "1" if enabled else "0")


@dataclass
class ActiveLine:
    account: FleetAccount
    engine: EngineProcess
    started_at: datetime
    last_bytes: int = 0
    last_progress_at: datetime | None = None
    last_accounted_at: datetime | None = None
    rate_samples: deque[tuple[datetime, int]] = field(default_factory=lambda: deque(maxlen=400))
    assigned_mbps: float = 0.0
    last_resize_at: datetime | None = None
    last_slow_notice_at: datetime | None = None


class FleetController(FleetSources):
    def __init__(
        self,
        config_path: str | Path,
        cfg: FleetConfig,
        *,
        remote_source_manager: RemoteSourceManager | None = None,
        snmp_reader: SnmpV2Reader | None = None,
    ) -> None:
        self.cfg = cfg.validate()
        self.config_path = Path(config_path)
        self.data_dir = self.config_path.parent
        self.store = FleetStore(self.data_dir / "fleet.sqlite3", self.cfg)
        self.lock = threading.RLock()
        # The environment controls the safe first-boot default. Once an
        # operator starts or stops the fleet, preserve that choice across an
        # unexpected container restart in the SQLite data volume.
        self.manual_enabled = self.store.load_manual_enabled(_environment_flag("FLEET_AUTOSTART", False))
        self.active: dict[str, ActiveLine] = {}
        self.logs: list[str] = []
        self.last_snmp_at = 0.0
        self.last_schedule_at = 0.0
        self.last_engine_at = 0.0
        self.snmp_status = "pending"
        self.snmp_quality: dict[str, str] = {}
        self.snmp_results: queue.SimpleQueue[tuple[list[SnmpInterface] | None, int | None, datetime, Exception | None]] = queue.SimpleQueue()
        self.snmp_thread: threading.Thread | None = None
        self.account_cooldowns: dict[str, datetime] = {}
        self.account_health_errors: dict[str, str] = {}
        self.rate_samples: deque[tuple[float, int]] = deque(maxlen=180)
        self.rate_total = 0
        self.wan_budget = WanBudget(interface=self.cfg.router_snmp_interface)
        self.daily_allocation: dict[str, float] = {}
        self.daily_schedule_key: tuple[str, str] | None = None
        self.daily_schedule_budget = 0.0
        self.daily_groups_changed = False
        self.view_cache_minute: int | None = None
        self.view_cache: dict[str, Any] = {}
        self.remote_source_manager = remote_source_manager
        self.remote_source_refresher = RemoteSourceRefreshWorker(remote_source_manager) if remote_source_manager is not None else None
        self.remote_source_snapshot = SourceListSnapshot(enabled=remote_source_manager is not None, status="disabled" if remote_source_manager is None else "pending")
        self.effective_source_pool = list(self.cfg.source_pool)
        self.effective_source_origin = "local-fallback"
        self._init_source_state()
        if remote_source_manager is not None:
            sources, snapshot = remote_source_manager.load_last_known_good()
            self.remote_source_snapshot = snapshot
            if sources:
                self.effective_source_pool = list(sources)
                self.effective_source_origin = "last-known-good"
                self.active_source_revision = snapshot.revision
                self.source_generation = self._source_generation(self.effective_source_pool, snapshot.revision)
            self.remote_source_refresher.request()
        self.snmp_reader = snmp_reader or self._reader_from_environment()
        inventory = os.environ.get("FLEET_ACCOUNTS_CSV", "").strip()
        if not self.store.accounts() and inventory:
            inventory_path = Path(inventory)
            if inventory_path.is_file() and inventory_path.stat().st_size:
                self.store.import_accounts(parse_accounts_csv(inventory_path.read_text(encoding="utf-8")))
            elif inventory_path.exists() and not inventory_path.is_file():
                raise ValueError("FLEET_ACCOUNTS_CSV must be a regular file")
            else:
                self.log("no bootstrap account CSV; waiting for Web API import")
        self._attach_addresses()
        self.dashboard_agent = None
        if os.environ.get("DASHBOARD_URL", "").strip():
            from .dashboard_agent import DashboardAgent
            try:
                self.dashboard_agent = DashboardAgent(self.data_dir)
            except Exception:
                self.log("dashboard initialization failed; check agent settings and restart to reconnect")

    def _reader_from_environment(self) -> SnmpV2Reader | None:
        host = os.environ.get("SNMP_HOST", "").strip()
        secret = os.environ.get("SNMP_COMMUNITY_FILE", "/run/secrets/ikuai_snmp_community")
        if not host:
            return None
        try:
            port = int(os.environ.get("SNMP_PORT", "161"))
        except ValueError as exc:
            raise ValueError("SNMP_PORT must be an integer") from exc
        return SnmpV2Reader(host, secret, port=port)

    def _attach_addresses(self) -> None:
        addresses = [item.bind_ip for item in self.store.accounts() if item.enabled]
        if addresses:
            apply_ipv4_addresses(addresses, os.environ.get("LAN_INTERFACE", "eth0"), os.environ.get("LAN_PREFIX", "24"), self.log)

    def tick(self, monotonic_now: float | None = None, wall_time: datetime | None = None) -> None:
        now_mono = time.monotonic() if monotonic_now is None else monotonic_now
        now = (datetime.now(SHANGHAI) if wall_time is None else wall_time.astimezone(SHANGHAI))
        self._poll_remote_sources(now_mono, now)
        force_schedule = self._consume_snmp_result(now)
        if now_mono - self.last_engine_at >= self.cfg.engine_status_seconds:
            self.last_engine_at = now_mono
            force_schedule = self._poll_engines(now) or force_schedule
        if now_mono - self.last_snmp_at >= self.cfg.snmp_poll_seconds:
            self.last_snmp_at = now_mono
            self._start_snmp_poll()
        key = (now.date().isoformat(), self._daily_phase(now))
        if key != self.daily_schedule_key or (self.active and self._router_snapshot(now)["phase_limit_mbps"] <= 0):
            force_schedule = True
        if force_schedule or now_mono - self.last_schedule_at >= self.cfg.scheduler_tick_seconds:
            self.last_schedule_at = now_mono
            self._schedule(now)
        if self.dashboard_agent is not None:
            try:
                self.dashboard_agent.tick(self, now, now_mono)
            except Exception:
                self.log("dashboard export failed; local scheduling continues")

    def _start_snmp_poll(self) -> None:
        if self.snmp_reader is None:
            self.snmp_status = "disabled"
            self._dashboard_upload_samples = {}
            return
        if self.snmp_thread is not None and self.snmp_thread.is_alive():
            return

        def collect() -> None:
            try:
                interfaces = self.snmp_reader.read()
                self.snmp_results.put((interfaces, getattr(self.snmp_reader, "last_sys_uptime", None), datetime.now(SHANGHAI), None))
            except Exception as exc:
                self.snmp_results.put((None, None, datetime.now(SHANGHAI), exc))

        try:
            thread = threading.Thread(target=collect, name="fleet-snmp", daemon=True)
            thread.start()
        except (RuntimeError, OSError):
            self.snmp_thread = None
            self.snmp_status = "degraded"
            self._dashboard_upload_samples = {}
            self.log("SNMP worker could not start; collection will retry without stopping the service")
            return
        self.snmp_thread = thread

    def _consume_snmp_result(self, now: datetime) -> bool:
        changed = False
        while True:
            try:
                interfaces, device_uptime, observed_at, error = self.snmp_results.get_nowait()
            except queue.Empty:
                break
            changed = True
            if error is not None or interfaces is None:
                self.snmp_status = "degraded"
                self._dashboard_upload_samples = {}
                self.log("SNMP collection failed; counters will be reconciled or estimated after recovery")
                continue
            try:
                prior_baselines = {}
                if self.dashboard_agent is not None:
                    prior_baselines = {row["account_id"]: row["baseline_at"] or row["sampled_at"]
                                       for row in self.store.db.execute("SELECT account_id,baseline_at,sampled_at FROM cursors")}
                self.snmp_quality = self.store.record_snmp(interfaces, observed_at, device_uptime=device_uptime)
                if self.dashboard_agent is not None:
                    corrected = [prior_baselines[account_id] for account_id, quality in self.snmp_quality.items()
                                 if quality in {"recovered", "reset"} and account_id in prior_baselines]
                    if corrected:
                        try:
                            self.dashboard_agent.invalidate_since(min(corrected))
                        except Exception:
                            self.log("dashboard correction queue failed; SNMP accounting continues")
                self.wan_budget.observe(interfaces, observed_at, device_uptime,
                                        swap=self.cfg.snmp_direction_swap, stale_seconds=self.cfg.wan_stale_seconds)
                self.snmp_status = "online"
                if self.dashboard_agent is not None:
                    try:
                        from .dashboard_export import current_upload_rates
                        # Capture every SNMP sample, including those between
                        # the slower dashboard exports, before cursors advance.
                        current_upload_rates(self, observed_at)
                    except Exception:
                        self._dashboard_upload_samples = {}
                        self.log("dashboard upload measurement failed; local scheduling continues")
                self.view_cache.clear()
            except Exception:
                self.snmp_status = "degraded"
                self._dashboard_upload_samples = {}
                self.log("SNMP accounting failed; sample was not committed")
        return changed

    def _poll_engines(self, now: datetime) -> bool:
        aggregate_delta = 0
        replacement_needed = False
        for account_id, active in list(self.active.items()):
            accounted_from = active.last_accounted_at or active.started_at
            self.store.record_runtime(account_id, accounted_from, now)
            active.last_accounted_at = now
            active.engine.poll()
            current = active.engine.state.total_bytes
            delta = max(0, current - active.last_bytes)
            active.last_bytes = current
            active.rate_samples.append((now, current))
            if delta:
                active.last_progress_at = now
                self.store.record_download(account_id, delta, now)
                aggregate_delta += delta
            if active.engine.state.status == "stopped":
                self.active.pop(account_id, None)
                replacement_needed = True
                continue
            failure = self._line_health_failure(active, now)
            if failure:
                self._quarantine(account_id, now, failure)
                replacement_needed = True
            elif self.cfg.rate_limit_enabled and (now - active.started_at).total_seconds() >= 60 and (
                active.last_resize_at is None or (now - active.last_resize_at).total_seconds() >= 15
            ):
                rate = self._active_rate(active, 60)
                current_connections = active.engine.state.connections
                target = current_connections
                if rate < active.assigned_mbps * 0.9:
                    target = min(self.cfg.max_connections_per_line, current_connections + 1)
                elif rate > active.assigned_mbps * 1.15:
                    target = max(1, current_connections - 1)
                if target != current_connections:
                    active.engine.set_connections(target)
                active.last_resize_at = now
        self.rate_total += aggregate_delta
        self.rate_samples.append((time.monotonic(), self.rate_total))
        return replacement_needed

    def _line_health_failure(self, active: ActiveLine, now: datetime) -> str:
        last_progress = active.last_progress_at or active.started_at
        stalled_for = max(0.0, (now - last_progress).total_seconds())
        if stalled_for >= self.cfg.line_stall_seconds:
            return f"no download progress for {int(stalled_for)}s"

        window = self.cfg.line_slow_window_seconds
        cutoff = now - timedelta(seconds=window)
        while len(active.rate_samples) > 2 and active.rate_samples[1][0] <= cutoff:
            active.rate_samples.popleft()
        if len(active.rate_samples) < 2:
            return ""
        first_at, first_bytes = active.rate_samples[0]
        last_at, last_bytes = active.rate_samples[-1]
        elapsed = max(0.0, (last_at - first_at).total_seconds())
        if elapsed < window:
            return ""
        mbps = max(0, last_bytes - first_bytes) * 8 / max(0.001, elapsed) / 1_000_000
        threshold = min(50.0, active.assigned_mbps * 0.25)
        if mbps < threshold:
            # The engine already rotates stalled/slow sources and probes their
            # recovery. Keep useful traffic flowing while it does so instead
            # of turning a slow source into a 30-minute whole-line outage.
            if active.last_slow_notice_at is None or (now - active.last_slow_notice_at).total_seconds() >= window:
                self.log(
                    f"account={active.account.account_id} slow download={mbps:.1f}Mbps "
                    f"threshold={threshold:g}Mbps; continuing download and automatic source recovery"
                )
                active.last_slow_notice_at = now
        return ""

    def _quarantine(self, account_id: str, now: datetime, reason: str) -> None:
        until = now + timedelta(minutes=self.cfg.line_failure_cooldown_minutes)
        self.account_cooldowns[account_id] = until
        self.account_health_errors[account_id] = reason
        self._stop(account_id)
        self.log(f"account={account_id} unhealthy reason={reason} cooldown_until={until.isoformat(timespec='seconds')}")

    def _schedule(self, now: datetime) -> None:
        self._schedule_daily(now)

    def _capture_daily_targets(self, now: datetime, forecasts: Mapping[str, Mapping[str, Any]]) -> None:
        snapshot = {account_id: int(values["target_download_bytes"]) for account_id, values in forecasts.items()}
        with self.store.db:
            self.store._set_meta(f"daily_forecast:{now.date().isoformat()}", json.dumps(snapshot))
            if now.hour < 18:
                self.store._set_meta(f"daily_pre18:{now.date().isoformat()}", json.dumps(snapshot))

    def _evening_override(self, now: datetime) -> dict[str, Any] | None:
        raw = self.store._meta("evening_override")
        if not raw or now.hour < 18:
            return None
        try:
            value = json.loads(raw)
            if value.get("date") == now.date().isoformat() and value.get("account_ids"):
                return value
        except (ValueError, AttributeError):
            pass
        return None

    def _daily_phase(self, now: datetime) -> str:
        if daily_phase(now) == "upload_only" and self._evening_override(now):
            return "evening_override"
        return daily_phase(now)

    def _router_snapshot(self, now: datetime) -> dict[str, Any]:
        value = self.wan_budget.snapshot(now, self._rates()[2], self.cfg)
        override = self._evening_override(now)
        if override and daily_phase(now) == "upload_only":
            value["phase_limit_mbps"] = min(self.cfg.late_download_limit_mbps, value["available_mbps"])
        value["evening_override"] = override
        return value

    def set_evening_override(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"date", "account_ids"}:
            raise ValueError("expected date and account_ids")
        now = datetime.now(SHANGHAI)
        ids = payload["account_ids"]
        if payload["date"] != now.date().isoformat():
            raise ValueError("evening override must use today's Shanghai date")
        if not isinstance(ids, list) or not ids or len(ids) > 20 or not all(isinstance(item, str) for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("account_ids must contain 1 to 20 unique account IDs")
        accounts = {item.account_id: item for item in self.store.accounts()}
        if any(account_id not in accounts or not accounts[account_id].enabled for account_id in ids):
            raise ValueError("override accounts must exist and be enabled")
        self.store.validate_daily_ready()
        expires = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), SHANGHAI)
        value = {"date": payload["date"], "account_ids": sorted(ids), "expires_at": expires.isoformat(),
                 "max_mbps": self.cfg.late_download_limit_mbps}
        with self.lock:
            with self.store.db:
                self.store._set_meta("evening_override", json.dumps(value))
            self.set_manual_enabled(True)
            self.daily_groups_changed = True
            self._schedule(now)
        self.log(f"temporary evening override accounts={','.join(sorted(ids))} expires={expires.isoformat()}")
        return value

    def _schedule_daily(self, now: datetime) -> None:
        forecasts = self._cached_view("forecast", now)
        self._capture_daily_targets(now, forecasts)
        budget = self._router_snapshot(now)["phase_limit_mbps"]
        key = (now.date().isoformat(), self._daily_phase(now))
        interrupt_minimum = (key != self.daily_schedule_key or self.daily_groups_changed
                             or budget + 0.001 < self.daily_schedule_budget)
        self.daily_schedule_key = key
        self.daily_schedule_budget = budget
        if not self.manual_enabled:
            self.daily_allocation = {}
            self._stop_all()
            return
        try:
            self.store.validate_daily_ready()
        except ValueError:
            self.daily_allocation = {}
            self._stop_all()
            return
        for account_id, until in list(self.account_cooldowns.items()):
            if until <= now:
                self.account_cooldowns.pop(account_id, None)
        accounts = [item for item in self.store.accounts() if item.enabled]
        unusable = {"pending", "baseline", "offline", "missing", "ambiguous"}
        eligible = [item for item in accounts if item.account_id not in self.account_cooldowns
                    and self.snmp_quality.get(item.account_id, "pending") not in unusable]
        override = self._evening_override(now)
        forced = set(override["account_ids"]) if override else set()
        if self._daily_phase(now) == "evening_override":
            eligible = [item for item in eligible if item.account_id in forced]
        minimum = timedelta(minutes=self.cfg.rotation_minutes)
        keep = [] if interrupt_minimum else [account_id for account_id, active in self.active.items()
                                            if now - active.started_at < minimum
                                            and active.engine.state.status not in {"stopped", "failed"}]
        allocation = allocate_daily(eligible, self.store.groups(), forecasts, budget,
                                    self.store.allocated_today(now), self.store.last_started(), keep, force=forced,
                                    surplus_seconds=(now.replace(hour=18, minute=0, second=0, microsecond=0) - now).total_seconds()
                                    if self._daily_phase(now) == "daytime" else 0)
        self.daily_groups_changed = False
        # Release old slots before starting new lines. iKuai, not the process,
        # enforces the per-line profile and aggregate physical limit.
        for account_id, active in list(self.active.items()):
            rate = allocation.get(account_id, 0.0)
            if rate <= 0 or rate != active.assigned_mbps:
                self._stop(account_id)
        by_id = {item.account_id: item for item in eligible}
        for account_id, rate in list(allocation.items()):
            active = self.active.get(account_id)
            if active is None:
                self._start(by_id[account_id], now, rate)
        self.daily_allocation = allocation

    def groups_snapshot(self) -> list[dict[str, Any]]:
        groups = []
        for group in self.store.groups():
            item = asdict(group)
            active = [line for line in self.active.values() if line.account.group_id == group.group_id]
            item.update(active_accounts=len(active), assigned_mbps=sum(line.assigned_mbps for line in active))
            groups.append(item)
        return groups

    def update_groups(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or set(payload) != {"groups"}:
            raise ValueError("expected {groups: [...]} payload")
        with self.lock:
            self.store.update_groups(payload["groups"])
            self.view_cache.clear()
            self.daily_groups_changed = True
            self._schedule(datetime.now(SHANGHAI))
            return self.groups_snapshot()

    def assign_groups(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or set(payload) != {"group_id", "account_ids"}:
            raise ValueError("expected group_id and account_ids")
        with self.lock:
            self.store.assign_group(payload["group_id"], payload["account_ids"])
            self.view_cache.clear()
            # Group identity in ActiveLine must follow the current inventory.
            by_id = {item.account_id: item for item in self.store.accounts()}
            for account_id, active in self.active.items():
                active.account = by_id[account_id]
            self.daily_groups_changed = True
            self._schedule(datetime.now(SHANGHAI))
            return self.groups_snapshot()

    def _cached_view(self, name: str, now: datetime) -> Any:
        minute = int(now.timestamp() // 60)
        if self.view_cache_minute != minute:
            self.view_cache_minute = minute
            self.view_cache.clear()
        if name not in self.view_cache:
            if name == "forecast":
                value = self.store.daily_forecasts(now)
            elif name == "rolling":
                value = self.store.rolling(now)
            elif name == "today":
                value = self.store.today(now)
            elif name == "pre18":
                value = self.store.daily_totals(now.date(), before_hour=18)
            else:
                raise ValueError("unknown cached view")
            self.view_cache[name] = value
        return self.view_cache[name]

    def _daily_snapshot(self, now: datetime) -> dict[str, Any]:
        forecasts = self._cached_view("forecast", now)
        enabled = {item.account_id for item in self.store.accounts() if item.enabled}
        rows = [values for account_id, values in forecasts.items() if account_id in enabled]
        sums = ("today_upload_bytes", "today_download_bytes", "measured_upload_bytes", "measured_download_bytes",
                "estimated_upload_bytes", "estimated_download_bytes", "unknown_seconds", "predicted_upload_bytes",
                "target_download_bytes", "remaining_download_bytes", "future_upload_bytes", "unknown_upload_budget_bytes")
        result: dict[str, Any] = {key: sum(item.get(key, 0) for item in rows) for key in sums}
        result.update(date=now.date().isoformat(), phase=self._daily_phase(now), total_accounts=len(rows),
                      evening_override=self._evening_override(now),
                      achieved_accounts=sum(item.get("remaining_download_bytes", 0) <= 0 for item in rows),
                      profile_unconfirmed_accounts=sum(item.enabled and not item.profile_confirmed for item in self.store.accounts()))
        result["pre18_result"] = None
        result["final_result"] = None
        pre_raw = self.store._meta(f"daily_pre18:{now.date().isoformat()}")
        if now.hour >= 18 and pre_raw:
            targets = json.loads(pre_raw)
            pre_totals = self._cached_view("pre18", now)
            ids = enabled & targets.keys()
            result["pre18_result"] = {
                "download_bytes": sum(pre_totals[account_id]["download"] for account_id in ids),
                "target_download_bytes": sum(targets[account_id] for account_id in ids),
                "achieved_accounts": sum(pre_totals[account_id]["download"] >= targets[account_id] for account_id in ids),
                "total_accounts": len(ids),
            }
        return result

    def _start(self, account: FleetAccount, now: datetime, assigned_mbps: float | None = None) -> None:
        rate = account.download_mbps if assigned_mbps is None else assigned_mbps
        line = LogicalLine(account.account_id, rate, account.bind_ip)
        sources = self.pending_source_pool or self.effective_source_pool
        generation = self.pending_source_generation or self.source_generation
        filename = hashlib.sha256(account.account_id.encode()).hexdigest()[:24] + ".json"
        engine = EngineProcess(self.cfg, line, sources, self.log,
                               sources_file=self.sources_runtime_dir / filename,
                               reject_private_destinations=True, source_generation=generation,
                               fallback_sources=self._fallback_sources(sources),
                               source_health=self.account_source_health.get(account.account_id))
        engine.start()
        self.active[account.account_id] = ActiveLine(
            account, engine, now, last_progress_at=now,
            rate_samples=deque([(now, 0)], maxlen=400),
            assigned_mbps=rate,
        )
        self.store.mark_started(account.account_id, now)
        self.log(f"account={account.account_id} start")

    def _stop(self, account_id: str) -> None:
        active = self.active.pop(account_id, None)
        if active is not None:
            active.engine.stop()
            self.account_source_health[account_id] = dict(getattr(active.engine, "source_health", {}))
            now = datetime.now(SHANGHAI)
            delta = max(0, active.engine.state.total_bytes - active.last_bytes)
            if delta:
                self.store.record_download(account_id, delta, now)
                self.rate_total += delta
                active.last_bytes += delta
            self.log(f"account={account_id} stop")

    def _stop_all(self) -> None:
        for account_id in list(self.active):
            self._stop(account_id)

    def start_downloads(self) -> None:
        self.set_manual_enabled(True)

    def stop_downloads(self) -> None:
        self.set_manual_enabled(False)

    def set_manual_enabled(self, enabled: bool) -> None:
        self.manual_enabled = enabled
        self.store.save_manual_enabled(enabled)
        if not enabled:
            self._stop_all()

    def shutdown(self) -> None:
        if self.dashboard_agent is not None:
            self.dashboard_agent.shutdown()
        self._stop_all()
        if self.remote_source_refresher is not None:
            self.remote_source_refresher.shutdown()
        self.store.close()

    def import_accounts_csv(self, payload: str) -> dict[str, Any]:
        accounts = parse_accounts_csv(payload)
        if any(item.enabled and not item.profile_confirmed for item in accounts):
            raise ValueError("daily mode requires weight=1 or weight=10 for every enabled account")
        known_groups = {group.group_id for group in self.store.groups()}
        if any(item.group_id not in known_groups for item in accounts):
            raise ValueError("CSV references an unknown group; create the group before importing")
        with self.lock:
            self._stop_all()
            self.store.import_accounts(accounts)
            self.view_cache.clear()
            self._attach_addresses()
        return {"ok": True, "accounts": len(accounts)}

    def update_config(self, data: dict[str, Any]) -> FleetConfig:
        allowed = {name for name, field in self.cfg.__dataclass_fields__.items() if field.init}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unsupported configuration fields: {', '.join(sorted(unknown))}")
        candidate = replace(self.cfg, **data).validate()
        if candidate.daily_mode_enabled:
            self.store.validate_daily_ready()
        wan_changed = (candidate.router_snmp_interface != self.cfg.router_snmp_interface
                       or candidate.snmp_direction_swap != self.cfg.snmp_direction_swap)
        source_pool_changed = candidate.source_pool != self.cfg.source_pool
        save_config(self.config_path, candidate)
        self.cfg = candidate
        self.store.cfg = candidate
        self.view_cache.clear()
        if wan_changed:
            self.wan_budget = WanBudget(interface=candidate.router_snmp_interface)
        self.daily_groups_changed = True
        self.last_schedule_at = 0
        if source_pool_changed:
            remote_active = self.remote_source_manager is not None and bool(self.remote_source_manager.current_urls)
            sources = (self.pending_source_pool or self.effective_source_pool) if remote_active else candidate.source_pool
            self._begin_source_update(list(sources), self.pending_source_revision or self.active_source_revision,
                                      self.effective_source_origin if remote_active else "local-config", time.monotonic())
        return candidate

    def _rates(self) -> tuple[float, float, float]:
        if len(self.rate_samples) < 2:
            return 0.0, 0.0, 0.0
        now_time, now_bytes = self.rate_samples[-1]
        previous_time, previous_bytes = self.rate_samples[-2]
        current = max(0, now_bytes - previous_bytes) * 8 / max(0.001, now_time - previous_time) / 1_000_000
        def average(seconds: int) -> float:
            start_time, start_bytes = self.rate_samples[0]
            for sample_time, sample_bytes in reversed(self.rate_samples):
                if now_time - sample_time >= seconds:
                    start_time, start_bytes = sample_time, sample_bytes
                    break
            return max(0, now_bytes - start_bytes) * 8 / max(0.001, now_time - start_time) / 1_000_000
        return current, average(10), average(60)

    def metrics(self) -> dict[str, Any]:
        now = datetime.now(SHANGHAI)
        current, avg10, avg60 = self._rates()
        totals = self._cached_view("rolling", now)
        today_totals = self._cached_view("today", now)
        download = sum(item["download"] for item in totals.values())
        upload = sum(item["upload"] for item in totals.values())
        today_download = sum(item["download"] for item in today_totals.values())
        today_upload = sum(item["upload"] for item in today_totals.values())
        target_mbps = sum(line.assigned_mbps for line in self.active.values())
        rolling_target_download = int(upload * self.cfg.target_ratio)
        today_target_download = int(today_upload * self.cfg.target_ratio)
        return {
            "target_mbps": target_mbps,
            "current_mbps": current, "avg10_mbps": avg10, "avg60_mbps": avg60,
            "today_bytes": today_download, "today_upload_bytes": today_upload,
            "target_percent": current / target_mbps * 100 if target_mbps else 0.0,
            "daily_target_percent": today_download / today_target_download * 100 if today_target_download else 0.0,
            "worker_count": len(self.active), "max_worker_count": 20,
            "capacity_warning": False, "rolling_upload_bytes": upload,
            "rolling_download_bytes": download,
            "rolling_ratio": download / upload if upload else None,
            "rolling_target_download_bytes": rolling_target_download,
            "rolling_debt_bytes": max(0, rolling_target_download - download),
            "rolling_ratio_progress_percent": download / rolling_target_download * 100 if rolling_target_download else 0.0,
            "rolling_measured_upload_bytes": sum(item["measured_upload"] for item in totals.values()),
            "rolling_measured_download_bytes": sum(item["measured_download"] for item in totals.values()),
            "rolling_estimated_upload_bytes": sum(item["estimated_upload"] for item in totals.values()),
            "rolling_estimated_download_bytes": sum(item["estimated_download"] for item in totals.values()),
            "rolling_legacy_upload_bytes": sum(item["legacy_upload"] for item in totals.values()),
            "rolling_legacy_download_bytes": sum(item["legacy_download"] for item in totals.values()),
            "rolling_program_download_bytes": sum(item["program_download"] for item in totals.values()),
            "rolling_unknown_seconds": sum(item["unknown_seconds"] for item in totals.values()),
            "rolling_interruptions": sum(item["interruptions"] for item in totals.values()),
            "daily_mode_enabled": self.cfg.daily_mode_enabled,
            "daily": self._daily_snapshot(now),
            "router_budget": self._router_snapshot(now),
            "groups": self.groups_snapshot(),
            "diagnostics": self._completion_diagnostics(now),
            "lines": self._line_metrics(now, totals, today_totals),
        }

    @staticmethod
    def _active_rate(active: ActiveLine, seconds: int) -> float:
        if len(active.rate_samples) < 2:
            return 0.0
        end_at, end_bytes = active.rate_samples[-1]
        start_at, start_bytes = active.rate_samples[0]
        for sample_at, sample_bytes in reversed(active.rate_samples):
            if (end_at - sample_at).total_seconds() >= seconds:
                start_at, start_bytes = sample_at, sample_bytes
                break
        elapsed = max(0.0, (end_at - start_at).total_seconds())
        return max(0, end_bytes - start_bytes) * 8 / max(0.001, elapsed) / 1_000_000

    def _line_metrics(
        self,
        now: datetime,
        totals: Mapping[str, Mapping[str, int]],
        today_totals: Mapping[str, Mapping[str, int]] | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor_status = self.store.cursor_status()
        forecasts = self._cached_view("forecast", now)
        for account in self.store.accounts():
            active = self.active.get(account.account_id)
            cooldown_until = self.account_cooldowns.get(account.account_id)
            current_mbps = self._active_rate(active, 5) if active else 0.0
            avg60_mbps = self._active_rate(active, 60) if active else 0.0
            values = totals[account.account_id]
            today_values = (today_totals or {}).get(account.account_id, {})
            upload = values["upload"]
            download = values["download"]
            cursor = cursor_status.get(account.account_id, {})
            result.append({
                "line_id": account.account_id, "account_id": account.account_id,
                "bind_ip": account.bind_ip, "snmp_interface": account.snmp_interface,
                "enabled": account.enabled, "active": active is not None,
                "weight": account.weight, "group_id": account.group_id,
                "profile_confirmed": account.profile_confirmed,
                "upload_mbps": account.upload_mbps, "download_mbps": account.download_mbps,
                "assigned_mbps": active.assigned_mbps if active else 0,
                "daily": forecasts.get(account.account_id, {}),
                "target_mbps": active.assigned_mbps if active else 0,
                "current_mbps": current_mbps, "avg60_mbps": avg60_mbps,
                "today_bytes": int(today_values.get("download", 0)),
                "today_upload_bytes": int(today_values.get("upload", 0)),
                "connections": active.engine.state.connections if active else 0,
                "max_connections": self.cfg.max_connections_per_line,
                "status": active.engine.state.status if active else ("cooldown" if cooldown_until else "idle"),
                "metrics_available": bool(active and active.engine.state.has_metrics),
                "capacity_warning": False,
                "last_error": active.engine.state.last_error if active else self.account_health_errors.get(account.account_id, ""),
                "cooldown_until": cooldown_until.isoformat(timespec="seconds") if cooldown_until else "",
                "health_state": "cooldown" if cooldown_until else ("active" if active else "eligible"),
                "rolling_upload_bytes": upload, "rolling_download_bytes": download,
                "measured_upload_bytes": values["measured_upload"],
                "measured_download_bytes": values["measured_download"],
                "estimated_upload_bytes": values["estimated_upload"],
                "estimated_download_bytes": values["estimated_download"],
                "legacy_upload_bytes": values["legacy_upload"],
                "legacy_download_bytes": values["legacy_download"],
                "program_download_bytes": values["program_download"],
                "unknown_seconds": values["unknown_seconds"],
                "counter_interruptions": values["interruptions"],
                "last_snmp_sample_at": (
                    datetime.fromtimestamp(int(cursor["sampled_at"]), SHANGHAI).isoformat(timespec="seconds")
                    if cursor.get("sampled_at") is not None else None
                ),
                "ratio": download / upload if upload else None,
                "debt_bytes": max(0, int(upload * self.cfg.target_ratio) - download),
                "snmp_quality": self.snmp_quality.get(account.account_id, "pending"),
            })
        return result

    def accounts_snapshot(self) -> list[dict[str, Any]]:
        now = datetime.now(SHANGHAI)
        totals = self._cached_view("rolling", now)
        return self._line_metrics(now, totals, self._cached_view("today", now))

    def report_csv(self, period: str = "rolling", date: str | None = None) -> str:
        if period == "daily":
            return self._daily_report_csv(date)
        if period != "rolling" or date is not None:
            raise ValueError("date is only supported for period=daily")
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "account_id", "bind_ip", "snmp_interface", "active",
            "rolling_upload_bytes", "rolling_download_bytes", "ratio", "debt_bytes",
            "measured_upload_bytes", "measured_download_bytes",
            "estimated_upload_bytes", "estimated_download_bytes",
            "legacy_upload_bytes", "legacy_download_bytes", "program_download_bytes",
            "unknown_seconds", "counter_interruptions", "snmp_quality",
        ])
        totals = self.store.rolling(datetime.now(SHANGHAI))
        for account in self.store.accounts():
            item = totals[account.account_id]
            ratio = item["download"] / item["upload"] if item["upload"] else ""
            writer.writerow([
                account.account_id, account.bind_ip, account.snmp_interface,
                int(account.account_id in self.active), item["upload"], item["download"], ratio,
                max(0, int(item["upload"] * self.cfg.target_ratio) - item["download"]),
                item["measured_upload"], item["measured_download"],
                item["estimated_upload"], item["estimated_download"],
                item["legacy_upload"], item["legacy_download"], item["program_download"],
                item["unknown_seconds"], item["interruptions"], self.snmp_quality.get(account.account_id, "pending"),
            ])
        return output.getvalue()

    def _daily_report_csv(self, date: str | None) -> str:
        now = datetime.now(SHANGHAI)
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date() if date is not None else now.date()
        except (ValueError, TypeError) as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        if day > now.date():
            raise ValueError("cannot report a future date")
        totals = self.store.daily_totals(day)
        before18 = self.store.daily_totals(day, before_hour=18)
        # Forecast snapshots are planning targets, never traffic ledger entries.
        pre_raw = self.store._meta(f"daily_pre18:{day.isoformat()}")
        final_raw = self.store._meta(f"daily_forecast:{day.isoformat()}")
        pre_targets = json.loads(pre_raw) if pre_raw else {}
        final_targets = json.loads(final_raw) if final_raw else {}
        live = self.store.daily_forecasts(now) if day == now.date() else {}
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["date", "account_id", "bind_ip", "snmp_interface", "weight", "group_id",
                         "upload_bytes", "download_bytes", "ratio", "target_download_bytes", "unmet_bytes",
                         "pre18_download_bytes", "pre18_target_bytes", "pre18_met", "full_day_met",
                         "measured_upload_bytes", "measured_download_bytes", "estimated_upload_bytes",
                         "estimated_download_bytes", "unknown_seconds", "program_download_bytes", "target_quality"])
        for account in self.store.accounts():
            values = totals[account.account_id]
            target = int(live.get(account.account_id, {}).get("target_download_bytes", final_targets.get(account.account_id, 0)))
            # Completed-day reports use actual known upload (+ conservative
            # unknown budget), not an obsolete earlier projection.
            if day != now.date():
                unknown_budget = values.get("unknown_upload_capacity", 0)
                target = int((values["upload"] + unknown_budget) * self.cfg.target_ratio)
            pre_target = pre_targets.get(account.account_id)
            down = values["download"]
            pre_down = before18[account.account_id]["download"]
            writer.writerow([day.isoformat(), account.account_id, account.bind_ip, account.snmp_interface,
                             account.weight or "", account.group_id, values["upload"], down,
                             down / values["upload"] if values["upload"] else "", target, max(0, target - down),
                             pre_down, pre_target if pre_target is not None else "",
                             int(pre_down >= pre_target) if pre_target is not None else "", int(down >= target),
                             values["measured_upload"], values["measured_download"], values["estimated_upload"],
                             values["estimated_download"], values["unknown_seconds"], values["program_download"],
                             "recorded" if final_raw or live else "no_forecast_snapshot"])
        return output.getvalue()

    def summary(self) -> dict[str, Any]:
        now = datetime.now(SHANGHAI)
        accounts = self.store.accounts()
        desired = len(self.daily_allocation)
        totals = self._cached_view("rolling", now)
        upload = sum(item["upload"] for item in totals.values())
        download = sum(item["download"] for item in totals.values())
        count = len(accounts)
        return {
            "active_accounts": len(self.active), "target_accounts": desired,
            "cooldown_accounts": len(self.account_cooldowns),
            "target_mbps": sum(line.assigned_mbps for line in self.active.values()),
            "snmp_status": self.snmp_status, "warmup": self.store.warmup(now),
            "rolling_upload_bytes": upload, "rolling_download_bytes": download,
            "rolling_ratio": download / upload if upload else None,
            "rolling_measured_upload_bytes": sum(item["measured_upload"] for item in totals.values()),
            "rolling_measured_download_bytes": sum(item["measured_download"] for item in totals.values()),
            "rolling_estimated_upload_bytes": sum(item["estimated_upload"] for item in totals.values()),
            "rolling_estimated_download_bytes": sum(item["estimated_download"] for item in totals.values()),
            "rolling_legacy_upload_bytes": sum(item["legacy_upload"] for item in totals.values()),
            "rolling_legacy_download_bytes": sum(item["legacy_download"] for item in totals.values()),
            "rolling_program_download_bytes": sum(item["program_download"] for item in totals.values()),
            "rolling_unknown_seconds": sum(item["unknown_seconds"] for item in totals.values()),
            "worst_case_ratio": (200 / count) if count else None,
            "daily_mode_enabled": self.cfg.daily_mode_enabled,
            "daily": self._daily_snapshot(now),
            "router_budget": self._router_snapshot(now),
            "groups": self.groups_snapshot(),
            "source_list": self.source_list_status(),
            "diagnostics": self._completion_diagnostics(now),
        }

    def status(self) -> dict[str, Any]:
        now = datetime.now(SHANGHAI)
        return {"topology": "fleet", "running": bool(self.active), "downloads_starting": False,
                "manual_enabled": self.manual_enabled, "within_window": True,
                "now": now.isoformat(timespec="seconds"), "config": self.cfg.to_dict(),
                "metrics": self.metrics(), "logs": self.logs[-120:]}

    def request_source_list_refresh(self, now: datetime | None = None) -> dict[str, Any]:
        if self.remote_source_refresher is None:
            raise ValueError("remote source list is disabled")
        self.remote_source_refresher.request(now)
        value = self.source_list_status()
        value["refresh_request_state"] = self.remote_source_refresher.status()
        return value

    refresh_source_list = request_source_list_refresh

    def log(self, message: str) -> None:
        self.logs.append(f"{datetime.now(SHANGHAI).isoformat(timespec='seconds')} {message}")
        self.logs = self.logs[-500:]
