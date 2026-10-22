"""Pure daily forecast calculations; predictions never enter the traffic ledger."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Mapping


def download_targets(*, upload_mbps: float, predicted_upload_bytes: int,
                     download_bytes: int, target_ratio: float) -> dict[str, Any]:
    """A whole-day six-hour nominal floor, with the forecast as the upper goal."""
    nominal_upload = int(upload_mbps * 1_000_000 / 8 * 6 * 3600)
    minimum = int(nominal_upload * target_ratio)
    predicted = int(predicted_upload_bytes * target_ratio)
    maximum = max(minimum, predicted)
    return {
        "minimum_upload_bytes": nominal_upload,
        "minimum_download_bytes": minimum,
        "predicted_download_bytes": predicted,
        "maximum_download_bytes": maximum,
        "target_download_bytes": maximum,
        "remaining_minimum_download_bytes": max(0, minimum - download_bytes),
        "minimum_achieved": download_bytes >= minimum,
        "remaining_download_bytes": max(0, maximum - download_bytes),
        "target_progress_percent": min(100.0, download_bytes * 100.0 / maximum) if maximum else 100.0,
    }


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
        samples.setdefault(key, []).append(max(0.0, float(row["upload"]) / capacity))
    result: dict[str, dict[int, float]] = {}
    for (account_id, interval), values in samples.items():
        if len(values) >= 3:
            result.setdefault(account_id, {})[interval] = float(median(values))
    return result


def evening_fallback(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Use measured evening bins when three historical days are not yet available."""
    samples: dict[str, list[float]] = {}
    for row in rows:
        if int(row["interval"]) < 72 or float(row["valid_seconds"]) < 720 or float(row["capacity"]) <= 0:
            continue
        samples.setdefault(str(row["account_id"]), []).append(max(0.0, float(row["upload"]) / float(row["capacity"])))
    return {key: max(1.0, float(median(values))) for key, values in samples.items() if len(values) >= 3}


def daily_prediction(
    now: datetime,
    *,
    upload_mbps: float,
    totals: Mapping[str, int],
    historical_utilization: Mapping[int, float],
    target_ratio: float,
    margin: float = 0.2,
    physical_upload_mbps: float | None = None,
    fallback_utilization: float = 1.0,
) -> dict[str, Any]:
    """Forecast the remaining local day; no prior-day debt is included."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    finish = midnight + timedelta(days=1)
    rate_bytes = upload_mbps * 1_000_000 / 8
    maximum_utilization = max(upload_mbps, physical_upload_mbps or upload_mbps) / upload_mbps
    future = 0.0
    predicted_seconds = 0.0
    fallback_seconds = 0.0
    cursor = now
    while cursor < finish:
        interval = (cursor.hour * 60 + cursor.minute) // 15
        boundary = midnight + timedelta(minutes=(interval + 1) * 15)
        duration = (boundary - cursor).total_seconds()
        if interval in historical_utilization:
            utilization = min(maximum_utilization, historical_utilization[interval] * (1.0 + margin))
            predicted_seconds += duration
        else:
            utilization = min(maximum_utilization, max(1.0, fallback_utilization) * (1.0 + margin)
                              if fallback_utilization > 1.0 else 1.0) if interval >= 18 * 4 else 0.0
            fallback_seconds += duration
        future += rate_bytes * utilization * duration
        cursor = boundary
    measured_upload = int(totals.get("measured_upload", 0))
    estimated_upload = int(totals.get("estimated_upload", 0))
    recorded_upload = int(totals.get("upload", measured_upload + estimated_upload))
    unknown_budget = max(0, int(totals.get("unknown_upload_capacity", 0)))
    predicted_upload = recorded_upload + unknown_budget + int(future)
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
        **download_targets(upload_mbps=upload_mbps, predicted_upload_bytes=predicted_upload,
                           download_bytes=download, target_ratio=target_ratio),
        "forecast_quality": quality,
        "forecast_historical_seconds": int(predicted_seconds),
        "forecast_fallback_seconds": int(fallback_seconds),
        "forecast_upload_ceiling_mbps": maximum_utilization * upload_mbps,
        "forecast_evening_fallback_mbps": upload_mbps * min(maximum_utilization, fallback_utilization),
        "ratio": download / recorded_upload if recorded_upload else None,
    }
