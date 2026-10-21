"""Conservatively repair legacy rejected download samples as 500 Mbps estimates.

Run without --apply to preview. Back up the database before applying. Measured
counters and program counters are never modified; each correction is audited
in the same transaction as its estimated-byte update.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from itertools import islice


def repair_estimates(db: sqlite3.Connection, cutoff_minute: int, *, apply: bool = False) -> dict:
    db.row_factory = sqlite3.Row
    if apply:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS metering_download_repairs (
                account_id TEXT NOT NULL, minute INTEGER NOT NULL,
                old_estimated_bytes INTEGER NOT NULL, new_estimated_bytes INTEGER NOT NULL,
                repaired_at INTEGER NOT NULL, method TEXT NOT NULL,
                PRIMARY KEY(account_id, minute)
            )""")
    candidates = db.execute("""
        SELECT t.account_id,t.minute,t.sample_seconds,t.measured_download_bytes,
               t.estimated_download_bytes,t.download_bytes
        FROM accounts a JOIN traffic t ON t.account_id=a.account_id
        WHERE a.weight=10 AND t.minute<? AND t.quality='anomaly'
          AND t.download_valid_seconds IS NULL
          AND t.sample_seconds BETWEEN 1 AND 60 AND t.estimated_download_bytes>0
          AND t.upload_valid_seconds>0
          AND t.upload_capacity_bytes>=t.upload_valid_seconds*62500000.0*0.99
        ORDER BY t.account_id,t.minute
    """, (cutoff_minute,))
    result = {"applied": apply, "changed_minutes": 0, "added_estimated_bytes": 0, "accounts": {}}
    while batch := list(islice(candidates, 500)):
        with db:
            for row in batch:
                # The nominal estimate is bounded by independently observed
                # program traffic in that minute, including mixed valid and
                # invalid samples. Existing measured bytes take precedence.
                capacity = row["sample_seconds"] * 62_500_000
                old = row["estimated_download_bytes"]
                new = max(old, min(capacity, row["download_bytes"]) - row["measured_download_bytes"])
                if new <= old:
                    continue
                if apply:
                    updated = db.execute("""
                        UPDATE traffic SET estimated_download_bytes=?
                        WHERE account_id=? AND minute=? AND estimated_download_bytes=?
                          AND download_bytes=? AND measured_download_bytes=? AND sample_seconds=?
                    """, (new, row["account_id"], row["minute"], old, row["download_bytes"],
                          row["measured_download_bytes"], row["sample_seconds"]))
                    if updated.rowcount != 1:
                        raise RuntimeError("traffic changed during repair; current batch rolled back")
                    db.execute("""INSERT INTO metering_download_repairs VALUES (?,?,?,?,?,?)""",
                               (row["account_id"], row["minute"], old, new, int(time.time()),
                                "nominal-500m-program-capped-v1"))
                result["changed_minutes"] += 1
                result["added_estimated_bytes"] += new - old
                item = result["accounts"].setdefault(row["account_id"], {"changed_minutes": 0, "added_estimated_bytes": 0})
                item["changed_minutes"] += 1
                item["added_estimated_bytes"] += new - old
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/data/fleet.sqlite3")
    parser.add_argument("--cutoff-minute", type=int, default=int(time.time() // 60) - 2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "rw" if args.apply else "ro"
    with sqlite3.connect(f"file:{args.database}?mode={mode}", uri=True, timeout=60) as db:
        print(json.dumps(repair_estimates(db, args.cutoff_minute, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
