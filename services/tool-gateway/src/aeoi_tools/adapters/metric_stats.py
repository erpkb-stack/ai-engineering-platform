"""Bucket size and series stats shared by every query_metrics backend (devdata, Prometheus):
the same input gives the same bucket and the same stats rule, whatever the source."""

from __future__ import annotations

import math
from datetime import datetime


def bucket_seconds(start: datetime, end: datetime, max_points: int) -> int:
    """Whole minutes, so buckets line up with a 1-minute source resolution; >= 60 s."""
    seconds = (end - start).total_seconds()
    return max(60, math.ceil(seconds / max_points / 60) * 60)


def series_stats(bucket_values: list[float]) -> dict[str, float | None]:
    """min/max/mean and nearest-rank p95 over BUCKET values (stated in the tool description)."""
    values = sorted(bucket_values)
    if not values:
        return {"min": None, "max": None, "mean": None, "p95": None}
    return {
        "min": round(values[0], 4),
        "max": round(values[-1], 4),
        "mean": round(sum(values) / len(values), 4),
        "p95": round(values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)], 4),
    }
