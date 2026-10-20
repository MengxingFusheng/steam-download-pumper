"""Read-only observation of source rollout and the first complete daily ledger.

The credential file uses URL, Username and Password lines, as maintained by the
existing Dashboard deployment. Credentials never appear in observation output.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def daily_result(node: dict, periods: list[dict], day: str) -> dict:
    closed = [p for p in periods if p.get("date") == day and p.get("mode") == "daily"
              and p.get("phase") == "late_makeup" and p.get("state") == "closed"]
    if not closed:
        return {"state": "unknown", "reason": "no_closed_daily_period"}
    enabled = {line["line_id"] for line in node.get("snapshot", {}).get("lines", []) if line.get("enabled")}
    period = max(closed, key=lambda item: item.get("end", 0))
    lines = []
    for line in period.get("lines", []):
        if line["line_id"] not in enabled:
            continue
        target, actual = line.get("target_bytes"), line.get("cumulative_actual_bytes")
        known = isinstance(target, (int, float)) and isinstance(actual, (int, float))
        lines.append({"line_id": line["line_id"], "target_bytes": target, "actual_bytes": actual,
                      "remaining_bytes": max(0, target - actual) if known else None,
                      "state": "unknown" if not known else "achieved" if actual >= target else "unmet"})
    complete = bool(enabled) and len(lines) == len(enabled) and all(line["state"] != "unknown" for line in lines)
    return {"state": "unknown" if not complete else "achieved" if all(line["state"] == "achieved" for line in lines) else "unmet",
            "basis": "latest_enabled_inventory; retained raw period for inventory-change review",
            "lines": lines, "period": period}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--until", required=True, help="Timezone-aware ISO timestamp")
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--expected-node", action="append", default=[], help="Upgraded node UUID; repeat as needed")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    finish = datetime.fromisoformat(args.until)
    if finish.tzinfo is None or args.interval < 15:
        parser.error("--until needs timezone and --interval must be at least 15 seconds")
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials = {key.strip(): value.strip() for key, separator, value in (
        line.partition(":") for line in args.credentials.read_text().splitlines()) if separator}
    origin = credentials["URL"].rstrip("/")
    token = base64.b64encode((credentials["Username"] + ":" + credentials["Password"]).encode()).decode()

    def get(path: str) -> dict:
        request = urllib.request.Request(origin + "/api/v1" + path, headers={"Authorization": "Basic " + token})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    expected = set(args.expected_node)
    observations = 0
    while True:
        now = datetime.now(SHANGHAI)
        try:
            nodes = get("/nodes?limit=100")["nodes"]
            rows = []
            for node in nodes:
                snapshot = node.get("snapshot", {})
                source = snapshot.get("source_list") or {}
                rows.append({"id": node["id"], "name": node["name"], "expected_upgraded": node["id"] in expected,
                             "online": node["online"], "last_seen": node["last_seen"], "mode": snapshot.get("mode"),
                             "manual_enabled": snapshot.get("manual_enabled"), "snmp_status": snapshot.get("snmp_status"),
                             "source_list": source, "summary": snapshot.get("summary"),
                             "diagnostics": snapshot.get("diagnostics", []),
                             "enabled_lines": [line["line_id"] for line in snapshot.get("lines", []) if line.get("enabled")]})
            sample = {"observed_at": now.isoformat(), "nodes": rows}
            observations += 1
            write_json(args.output / "latest.json", sample)
        except Exception as exc:
            sample = {"observed_at": now.isoformat(), "error_type": type(exc).__name__}
        with (args.output / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
        if args.once:
            print(json.dumps({"observations": observations, "output": str(args.output), "error_type": sample.get("error_type")}))
            return 0 if observations else 1
        if now >= finish:
            break
        time.sleep(min(args.interval, max(0.0, (finish - now).total_seconds())))
    report = {"date": args.report_date, "finished_at": datetime.now(SHANGHAI).isoformat(),
              "observations": observations, "nodes": []}
    try:
        for node in get("/nodes?limit=100")["nodes"]:
            periods = get(f"/nodes/{node['id']}/periods?day={args.report_date}")["periods"]
            report["nodes"].append({"id": node["id"], "name": node["name"],
                                    "expected_upgraded": node["id"] in expected,
                                    **daily_result(node, periods, args.report_date)})
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    write_json(args.output / "daily-result.json", report)
    print(json.dumps({"report": str(args.output / "daily-result.json"), "observations": observations}))
    return 0 if "error_type" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
