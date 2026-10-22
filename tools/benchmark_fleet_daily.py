"""Opt-in SQLite load test: python3 tools/benchmark_fleet_daily.py.

Creates only an automatically cleaned temporary database, never production data.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steam_pumper.config import FleetConfig
from steam_pumper.fleet import FleetAccount, FleetStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, default=256)
    parser.add_argument("--days", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.accounts <= 256 or not 8 <= args.days <= 30:
        parser.error("accounts must be 1–256; days must be 8–30")
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(zone).replace(hour=12, minute=0, second=0, microsecond=0)
    midnight = now.replace(hour=0)
    lower = int((midnight - timedelta(days=args.days - 1)).timestamp() // 60)
    with tempfile.TemporaryDirectory(prefix="fleet-daily-benchmark-") as directory:
        store = FleetStore(Path(directory) / "fleet.sqlite3", FleetConfig().validate())
        store.db.execute("PRAGMA cache_size=-65536")
        store.import_accounts([
            FleetAccount(f"a{index:03d}", f"10.23.{index // 250}.{index % 250 + 1}", f"pppoe-{index}", True,
                         10 if index % 4 == 0 else 1, "symmetric500" if index % 4 == 0 else "standard")
            for index in range(args.accounts)
        ])
        seeded = time.monotonic()
        for offset in range(args.days):
            start = lower + offset * 1440
            with store.db:
                store.db.execute("""
                WITH RECURSIVE minutes(value) AS (
                  SELECT ? UNION ALL SELECT value+1 FROM minutes WHERE value<?
                )
                INSERT INTO traffic(account_id,minute,measured_upload_bytes,measured_download_bytes,
                  download_bytes,sample_seconds,upload_valid_seconds,upload_capacity_bytes)
                SELECT account_id,value,
                  CASE WHEN weight=10 THEN 1875000000 ELSE 187500000 END,
                  CASE WHEN weight=10 THEN 3750000000 ELSE 375000000 END,
                  CASE WHEN weight=10 THEN 3700000000 ELSE 370000000 END,60,60,
                  CASE WHEN weight=10 THEN 3750000000 ELSE 375000000 END
                FROM accounts CROSS JOIN minutes
                """, (start, start + 1439))
            print(json.dumps({"seeded_days": offset + 1, "elapsed_seconds": round(time.monotonic() - seeded, 3)}), flush=True)
        row_count = store.db.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
        results = {"accounts": args.accounts, "days": args.days, "rows": row_count,
                   "seed_seconds": round(time.monotonic() - seeded, 3)}
        for label, action in (
            ("hourly_history_seconds", lambda: store._historical_utilization(now)),
            ("daily_forecast_seconds", lambda: store.daily_forecasts(now)),
            ("cached_daily_forecast_seconds", lambda: store.daily_forecasts(now + timedelta(minutes=1))),
            ("daily_totals_seconds", lambda: store.daily_totals(now.date())),
            ("rolling_totals_seconds", lambda: store.rolling(now)),
        ):
            started = time.monotonic()
            value = action()
            results[label] = round(time.monotonic() - started, 4)
            assert len(value) == args.accounts, (label, len(value))
        results["database_bytes"] = store.path.stat().st_size
        print(json.dumps(results), flush=True)
        store.close()


if __name__ == "__main__":
    main()
