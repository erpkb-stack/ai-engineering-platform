"""Anomaly maths for the metrics agent. Pure functions, no IO, no LLM (architecture §10 #4).

Method: robust z-score of each in-window bucket against a BASELINE taken from the same series
before the window:  z = (x - median(baseline)) / scale,  scale = 1.4826 * MAD.

Why median/MAD and not mean/stddev: one spike inside the baseline inflates the stddev and
hides the real anomaly; the median and MAD barely move. Why a floor on the scale: a flat
baseline (MAD = 0) would make every tiny wiggle "infinitely" anomalous.

Why also an ABSOLUTE floor per metric (review finding): a count that is mostly 0 has median
0 and MAD 0; with only a relative floor, 0.2 errors/min became "robust z +200000".

A RUN is consecutive in-window buckets with |z| >= threshold and the same sign.
- shift:     a run of >= min_shift buckets still going at the last in-window bucket
- transient: a run of >= min_run buckets that ENDED (back within baseline), or a short run
             still going at the window end (`ongoing`; too short to call a shift - the fact
             says so instead of claiming it recovered: review finding)
Single-bucket runs are counted, not reported (one sample is not evidence of a change).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Baseline:
    median: float
    scale: float
    n: int


@dataclass(frozen=True)
class Run:
    kind: str  # "shift" | "transient"
    direction: str  # "up" | "down"
    onset: datetime
    last: datetime
    buckets: int
    peak_value: float
    peak_at: datetime
    peak_z: float
    ongoing: bool = False  # still anomalous at the last in-window bucket


@dataclass(frozen=True)
class SeriesVerdict:
    baseline: Baseline | None
    runs: list[Run]
    single_spikes: int
    window_points: int
    window_median: float | None
    max_abs_z: float | None
    reason: str | None = None  # why no verdict (e.g. baseline too short)
    # the last in-window bucket: facts say "to <this>", not "to the window end" - during a
    # live incident the window end is in the future and has no data (review finding)
    last_ts: datetime | None = None


def baseline_of(values: list[float], rel_floor: float, abs_floor: float = 1e-6) -> Baseline:
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    scale = max(MAD_TO_SIGMA * mad, rel_floor * abs(med), abs_floor)
    return Baseline(median=med, scale=scale, n=len(values))


def analyse(
    points: list[tuple[datetime, float]],
    window_start: datetime,
    *,
    z_threshold: float = 4.0,
    min_shift: int = 3,
    min_run: int = 2,
    min_baseline: int = 10,
    rel_floor: float = 0.02,
    abs_floor: float = 1e-6,
    bucket: timedelta = timedelta(0),
) -> SeriesVerdict:
    """`points` sorted by time (bucket START times); buckets that END by `window_start` are
    the baseline. A bucket straddling the window start is in neither (review finding: with a
    baseline length that is not a multiple of the bucket, it mixed window minutes in)."""
    base_vals = [v for t, v in points if t + bucket <= window_start]
    win = [(t, v) for t, v in points if t >= window_start]
    if len(base_vals) < min_baseline:
        return SeriesVerdict(
            None, [], 0, len(win), None, None,
            reason=f"baseline too short ({len(base_vals)} < {min_baseline} buckets)",
        )  # fmt: skip
    if not win:
        return SeriesVerdict(None, [], 0, 0, None, None, reason="no points in the window")
    b = baseline_of(base_vals, rel_floor, abs_floor)
    zs = [(t, v, (v - b.median) / b.scale) for t, v in win]
    runs: list[Run] = []
    singles = 0
    i = 0
    while i < len(zs):
        z = zs[i][2]
        if abs(z) < z_threshold:
            i += 1
            continue
        sign = z > 0
        j = i
        while j + 1 < len(zs) and abs(zs[j + 1][2]) >= z_threshold and (zs[j + 1][2] > 0) == sign:
            j += 1
        seg = zs[i : j + 1]
        n = len(seg)
        if n < min_run:
            singles += 1
        else:
            peak = max(seg, key=lambda p: abs(p[2]))
            ongoing = j == len(zs) - 1
            runs.append(
                Run(
                    kind="shift" if ongoing and n >= min_shift else "transient",
                    direction="up" if sign else "down",
                    onset=seg[0][0],
                    last=seg[-1][0],
                    buckets=n,
                    peak_value=peak[1],
                    peak_at=peak[0],
                    peak_z=peak[2],
                    ongoing=ongoing,
                )
            )
        i = j + 1
    return SeriesVerdict(
        baseline=b,
        runs=runs,
        single_spikes=singles,
        window_points=len(win),
        window_median=statistics.median(v for _, v in win),
        max_abs_z=max(abs(z) for _, _, z in zs),
        last_ts=win[-1][0],
    )
