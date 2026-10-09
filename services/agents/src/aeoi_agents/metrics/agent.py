"""Metrics agent (architecture.md §10 #4). Code only - no LLM call (ADR-020).

  1 tools (code): query_metrics per (service, metric), baseline + window in ONE query
  2 maths (code): robust z-score per bucket against the pre-window baseline (anomaly.py)
  3 facts (code): one Fact per series - an anomaly (onset, peak, z) OR "stayed within its
                  baseline" - each citing the series' evidence id

"Stayed within baseline" is a fact on purpose: "requests_per_sec did not move" is what rules
out a traffic surge later (Phase 11). A metric the service does not report is a NOTE: no
evidence exists to cite, and absence of a series is not a measurement.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aeoi_agents.common import Failure, call, context, flagged_ids, hhmm, ts
from aeoi_agents.config import MetricsBudget
from aeoi_agents.metrics.anomaly import Baseline, Run, SeriesVerdict, analyse
from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import AgentRunResult, AgentTask, EvidenceItem, MetricAnomaly
from aeoi_models.findings import EvidenceKind, EvidenceRef, Fact
from aeoi_tool_client import ToolClient

MAX_QUERY = timedelta(hours=24)  # tool-gateway TimeWindow limit
AGENT_NAME = "metrics"
AGENT_VERSION = "1.0.0"
SOURCE = "devdata.metric_points"
# golden signals first, then saturation: if the call budget runs out, it cuts the least useful
METRIC_ORDER = (
    "http_5xx_per_min",
    "p95_latency_ms",
    "requests_per_sec",
    "cpu_util",
    "db_pool_utilization",
    "thread_pool_utilization",
    "db_connections_active",
    "db_query_latency_ms",
    "cache_hit_ratio",
)


# Smallest change that can count as one "sigma" per metric (review finding: a mostly-zero
# count made 0.2 errors/min an anomaly). Units: the metric's own.
ABS_FLOOR = {
    "http_5xx_per_min": 1.0,
    "p95_latency_ms": 5.0,
    "requests_per_sec": 1.0,
    "cpu_util": 0.01,
    "db_pool_utilization": 0.01,
    "thread_pool_utilization": 0.01,
    "db_connections_active": 1.0,
    "db_query_latency_ms": 1.0,
    "cache_hit_ratio": 0.005,
}


@dataclass
class Deps:
    tools: ToolClient
    budget: MetricsBudget


def _fmt(v: float) -> str:
    return f"{v:.4g}"


class MetricsAgent:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def run(
        self, task: AgentTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        b = self.deps.budget
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        ctx = context(task, AGENT_NAME, user_token, correlation_id)
        deadline = time.monotonic() + b.task_deadline_s
        # the gateway caps one query at 24 h: shorten the baseline for long windows (review)
        baseline = min(timedelta(minutes=b.baseline_min), MAX_QUERY - (task.end - task.start))
        query_start = task.start - baseline
        facts: list[Fact] = []
        notes: list[str] = []
        anomalies: list[MetricAnomaly] = []
        evidence: list[EvidenceItem] = []
        missing: dict[str, list[str]] = {}
        denied: Failure | None = None
        failed: list[str] = []
        skipped: list[str] = []
        for svc in task.service_keys:
            for metric in METRIC_ORDER:
                if denied is not None:
                    break
                if len(tracer.trace.tool_calls) >= b.max_tool_calls:
                    skipped.append(f"{svc}/{metric}")
                    continue
                if time.monotonic() > deadline:
                    skipped.append(f"{svc}/{metric}")
                    continue
                res = await call(
                    self.deps.tools,
                    tracer,
                    "query_metrics",
                    {
                        "service_key": svc,
                        "metric": metric,
                        "start": query_start.isoformat(),
                        "end": task.end.isoformat(),
                        "max_points": b.max_points,
                    },
                    ctx,
                )
                if isinstance(res, Failure):
                    if res.denied:
                        denied = res  # same answer for every metric: don't ask 17 more times
                    else:
                        failed.append(f"{svc}/{metric} ({res.reason})")
                    continue
                items = res.data.get("items", [])
                if not items:
                    missing.setdefault(svc, []).append(metric)
                    continue
                item = items[0]
                points = sorted((ts(p["ts"]), float(p["value"])) for p in item["points"])
                verdict = analyse(
                    points,
                    task.start,
                    z_threshold=b.z_threshold,
                    min_shift=b.min_shift_buckets,
                    abs_floor=ABS_FLOOR.get(metric, 1e-6),
                    bucket=timedelta(seconds=int(item["bucket_seconds"])),
                )
                base = verdict.baseline
                if base is None:
                    notes.append(f"{svc} {metric}: not judged - {verdict.reason}.")
                    continue
                ev_id = str(item["evidence_id"])
                evidence.append(
                    _evidence(ev_id, svc, metric, int(item["bucket_seconds"]), points, verdict,
                              base, res.tool_call_id, ev_id in flagged_ids(res))
                )  # fmt: skip
                for run in verdict.runs:
                    anomalies.append(_anomaly(svc, metric, run, base, item, ev_id))
                facts += self._facts(task, svc, metric, verdict, base, ev_id)
                if verdict.single_spikes:
                    notes.append(
                        f"{svc} {metric}: {verdict.single_spikes} single-bucket spike(s) ignored."
                    )
        if denied is not None and not evidence:
            return AgentRunResult(
                status="FAILED",
                error=f"no metric data: query_metrics denied ({denied.reason})",
                trace=tracer.finish(),
            )
        if not evidence and not missing and failed:
            return AgentRunResult(
                status="FAILED",
                error="no metric data: every query_metrics call failed",
                trace=tracer.finish(),
            )
        for svc, names in missing.items():
            notes.append(f"{svc}: no series for {', '.join(names)} (not reported by this service).")
        if len(facts) > b.max_facts:
            notes.append(f"{len(facts) - b.max_facts} steady-metric fact(s) dropped (budget).")
            facts = _keep_anomalies_first(facts, b.max_facts)
        degraded = "; ".join(
            filter(
                None,
                [
                    f"failed: {', '.join(failed)}" if failed else "",
                    f"not checked (budget/deadline): {', '.join(skipped)}" if skipped else "",
                    f"denied: {denied.reason}" if denied else "",
                ],
            )
        )
        cited = {r.evidence_id for f in facts for r in f.evidence}
        return AgentRunResult(
            status="SUCCEEDED",
            degraded=degraded or None,
            facts=facts,
            notes=notes[:30],
            anomalies=_cited_only(anomalies, cited)[:50],
            evidence=[e for e in evidence if e.evidence_key in cited],
            trace=tracer.finish(),
        )

    def _facts(
        self,
        task: AgentTask,
        svc: str,
        metric: str,
        v: SeriesVerdict,
        baseline: Baseline,
        ev_id: str,
    ) -> list[Fact]:
        base = (
            f"baseline median {_fmt(baseline.median)} over {baseline.n} buckets "
            f"before {hhmm(task.start)}"
        )
        last = hhmm(v.last_ts) if v.last_ts else hhmm(task.end)
        if not v.runs:
            if (v.max_abs_z or 0) < self.deps.budget.z_threshold:
                return [
                    _fact(
                        f"{svc} {metric}: stayed within its baseline from {hhmm(task.start)} to "
                        f"{last} (window median {_fmt(v.window_median or 0)}, {base}, "
                        f"max |robust z| {v.max_abs_z or 0:.1f} < "
                        f"{self.deps.budget.z_threshold:g}).",
                        ev_id,
                    )
                ]
            # only single-bucket spikes: NOT "within baseline" (review finding)
            return [
                _fact(
                    f"{svc} {metric}: no sustained change from {hhmm(task.start)} to {last}, but "
                    f"{v.single_spikes} single-bucket spike(s), max |robust z| "
                    f"{v.max_abs_z or 0:.1f} (window median {_fmt(v.window_median or 0)}, {base}).",
                    ev_id,
                )
            ]
        out = []
        for r in v.runs:
            if r.kind == "shift":
                shape = f"sustained shift {r.direction} from {hhmm(r.onset)} to {last}"
            elif r.ongoing:
                shape = (
                    f"{r.direction} from {hhmm(r.onset)}, still anomalous at {last} "
                    f"({r.buckets} buckets: too short to call a sustained shift)"
                )
            else:
                shape = (
                    f"transient {r.direction} from {hhmm(r.onset)} to {hhmm(r.last)} "
                    f"({r.buckets} buckets), then back within baseline"
                )
            out.append(
                _fact(
                    f"{svc} {metric}: {shape}; peak {_fmt(r.peak_value)} at {hhmm(r.peak_at)} "
                    f"(robust z {r.peak_z:+.1f}), {base}.",
                    ev_id,
                )
            )
        return out


def _fact(statement: str, ev_id: str) -> Fact:
    return Fact(
        statement=statement[:2000],
        produced_by=AGENT_NAME,
        evidence=[EvidenceRef(evidence_id=ev_id, kind=EvidenceKind.METRIC, source_system=SOURCE)],
    )


def _keep_anomalies_first(facts: list[Fact], n: int) -> list[Fact]:
    hot = [f for f in facts if "stayed within" not in f.statement]
    cold = [f for f in facts if "stayed within" in f.statement]
    return (hot + cold)[:n]


def _cited_only(anomalies: list[MetricAnomaly], cited: set[str]) -> list[MetricAnomaly]:
    """An anomaly must cite stored evidence: drop those whose fact fell to the budget."""
    return [a for a in anomalies if set(a.evidence_ids) <= cited]


def _anomaly(
    svc: str, metric: str, r: Run, b: Baseline, item: dict[str, Any], ev_id: str
) -> MetricAnomaly:
    return MetricAnomaly(
        service_key=svc,
        metric=metric,
        kind=r.kind,  # type: ignore[arg-type]
        direction=r.direction,  # type: ignore[arg-type]
        onset=r.onset,
        last_anomalous=r.last,
        baseline_median=round(b.median, 6),
        baseline_scale=round(b.scale, 6),
        peak_value=r.peak_value,
        peak_at=r.peak_at,
        peak_z=round(r.peak_z, 2),
        anomalous_buckets=r.buckets,
        bucket_seconds=int(item["bucket_seconds"]),
        evidence_ids=[ev_id],
    )


def _evidence(
    ev_id: str,
    svc: str,
    metric: str,
    bucket_s: int,
    points: list[Any],
    v: SeriesVerdict,
    b: Baseline,
    tool_call_id: Any,
    flagged: bool,
) -> EvidenceItem:
    """The stored excerpt: baseline stats + the window points (capped), as JSON - enough to
    re-check the fact by hand without re-running the query."""
    if v.runs:  # 5 buckets of context before the first onset, then the anomaly
        since = v.runs[0].onset - timedelta(seconds=5 * bucket_s)
        win = [(t, x) for t, x in points if t >= since]
    else:
        win = points[-60:]
    body = {
        "service": svc,
        "metric": metric,
        "bucket_seconds": bucket_s,
        "baseline": {
            "median": round(b.median, 6),
            "scale": round(b.scale, 6),
            "buckets": b.n,
        },
        "points": [[hhmm(t), round(x, 4)] for t, x in win[:90]],
        "flagged": flagged,
    }
    excerpt = json.dumps(body, separators=(",", ":"))[:4000]
    first = v.runs[0].onset if v.runs else points[-1][0]
    return EvidenceItem(
        evidence_key=ev_id,
        kind="METRIC",
        source_system=SOURCE,
        title=f"{svc} {metric} ({len(v.runs)} anomaly run(s))"[:300],
        excerpt=excerpt,
        content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        observed_at=first,
        tool_call_id=tool_call_id,
    )
