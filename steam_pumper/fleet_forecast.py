"""Pure daily forecast calculations; predictions never enter the traffic ledger."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Mapping


def history_model(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[int, float]]:
    """Normalize each historical day using its recorded bandwidth profile.

    A 15-minute bin needs at least 12 minutes of measured upload, and at least
    three complete days must qualify before a median is considered reliable.
    """
    samples: dict[tuple[str, int], list[float]] = {}
    for row in rows:
        capacity = float(row["capacity"])
        if float(row["valid_seconds"]) < 12 * 60 or capacity <= 0:
            continue
        key = (str(row["account_id"]), int(row["interval"]))
        samples.setdefault(key, []).append(max(0.0, min(1.0, float(row["upload"]) / capacity)))
    result: dict[str, dict[int, float]] = {}
    for (account_id, interval), values in samples.items():
        if len(values) >= 3:
            result.setdefault(account_id, {})[interval] = float(median(values))
    return result


def daily_prediction(
    now: datetime,
    *,
    upload_mbps: float,
    totals: Mapping[str, int],
    historical_utilization: Mapping[int, float],
    target_ratio: float,
    margin: float = 0.2,
) -> dict[str, Any]:
    """Forecast the remaining local day; no prior-day debt is included."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    finish = midnight + timedelta(days=1)
    rate_bytes = upload_mbps * 1_000_000 / 8
    future = 0.0
    predicted_seconds = 0.0
    fallback_seconds = 0.0
    cursor = now
    while cursor < finish:
        interval = (cursor.hour * 60 + cursor.minute) // 15
        boundary = midnight + timedelta(minutes=(interval + 1) * 15)
        duration = (boundary - cursor).total_seconds()
        if interval in historical_utilization:
            utilization = min(1.0, historical_utilization[interval] * (1.0 + margin))
            predicted_seconds += duration
        else:
            utilization = 1.0 if interval >= 18 * 4 else 0.0
            fallback_seconds += duration
        future += rate_bytes * utilization * duration
        cursor = boundary
    measured_upload = int(totals.get("measured_upload", 0))
    estimated_upload = int(totals.get("estimated_upload", 0))
    recorded_upload = int(totals.get("upload", measured_upload + estimated_upload))
    unknown_budget = max(0, int(totals.get("unknown_upload_capacity", 0)))
    predicted_upload = recorded_upload + unknown_budget + int(future)
    target = int(predicted_upload * target_ratio)
    download = int(totals.get("download", 0))
    quality = "historical" if fallback_seconds == 0 else ("cold-start" if predicted_seconds == 0 else "mixed")
    return {
        "today_upload_bytes": recorded_upload,
        "today_download_bytes": download,
        "today_measured_upload_bytes": measured_upload,
        "today_measured_download_bytes": int(totals.get("measured_download", 0)),
        "today_estimated_upload_bytes": estimated_upload,
        "today_estimated_download_bytes": int(totals.get("estimated_download", 0)),
        "today_program_download_bytes": int(totals.get("program_download", 0)),
        "today_unknown_seconds": int(totals.get("unknown_seconds", 0)),
        "today_unknown_upload_seconds": int(totals.get("unknown_upload_seconds", 0)),
        "unknown_upload_budget_bytes": unknown_budget,
        "future_upload_bytes": int(future),
        "predicted_upload_bytes": predicted_upload,
        "target_download_bytes": target,
        "remaining_download_bytes": max(0, target - download),
        "forecast_quality": quality,
        "forecast_historical_seconds": int(predicted_seconds),
        "forecast_fallback_seconds": int(fallback_seconds),
        "ratio": download / recorded_upload if recorded_upload else None,
        "target_progress_percent": min(100.0, download * 100.0 / target) if target else 100.0,
    }
