"""Phase 10 code-only agents (metrics, deployment, knowledge) with fake tools. No IO, no LLM.
Assert facts, evidence ids and invariants (.claude/rules/testing.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from aeoi_agents.config import DeploymentBudget, KnowledgeBudget, MetricsBudget
from aeoi_agents.deployment.agent import DeploymentAgent
from aeoi_agents.deployment.agent import Deps as DDeps
from aeoi_agents.knowledge.agent import Deps as KDeps
from aeoi_agents.knowledge.agent import KnowledgeAgent, query_for
from aeoi_agents.metrics.agent import METRIC_ORDER, MetricsAgent
from aeoi_agents.metrics.agent import Deps as MDeps
from aeoi_agents.metrics.anomaly import analyse, baseline_of
from aeoi_models.api.agents import AgentTask
from aeoi_tool_client import FakeToolClient, ToolCallError

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)  # window start
WINDOW_END = T0 + timedelta(minutes=90)


def task(**kw: Any) -> AgentTask:
    return AgentTask(
        incident_id=uuid4(),
        investigation_id=uuid4(),
        service_keys=kw.pop("services", ["checkout-api"]),
        start=T0,
        end=WINDOW_END,
        **kw,
    )


def series(fn: Any, minutes: int = 210, start: datetime | None = None) -> list[dict[str, Any]]:
    """1-minute points from 120 min before the window to its end; fn(minute_offset) -> value,
    offset 0 = window start. A small deterministic wiggle keeps the MAD non-zero."""
    t = start or T0 - timedelta(minutes=120)
    out = []
    for i in range(minutes):
        off = i - 120
        wiggle = ((i * 7) % 5 - 2) * 0.01  # -2%..+2%
        out.append({"ts": (t + timedelta(minutes=i)).isoformat(), "value": fn(off) * (1 + wiggle)})
    return out


def metrics_reply(points: list[dict[str, Any]], metric: str = "p95_latency_ms") -> dict[str, Any]:
    return {
        "_kind": "METRIC",
        "items": [
            {
                "service_key": "checkout-api",
                "metric": metric,
                "bucket_seconds": 60,
                "points": points,
                "stats": {},
            }
        ],
    }


# ------------------------------------------------------------------ anomaly maths (pure)
def pts(values: list[float], start: datetime) -> list[tuple[datetime, float]]:
    return [(start + timedelta(minutes=i), v) for i, v in enumerate(values)]


def test_shift_onset_is_the_first_anomalous_bucket() -> None:
    base = [100 + (i % 3) for i in range(60)]
    window = [100.0] * 20 + [240.0] * 30
    v = analyse(pts(base + window, T0 - timedelta(minutes=60)), T0)
    assert len(v.runs) == 1
    r = v.runs[0]
    assert r.kind == "shift" and r.direction == "up"
    assert r.onset == T0 + timedelta(minutes=20)
    assert r.buckets == 30


def test_a_recovered_blip_is_transient_not_a_shift() -> None:
    base = [0.94 + (i % 3) * 0.002 for i in range(60)]
    window = [0.94] * 10 + [0.70] * 3 + [0.94] * 20
    v = analyse(pts(base + window, T0 - timedelta(minutes=60)), T0)
    assert [(r.kind, r.direction, r.buckets) for r in v.runs] == [("transient", "down", 3)]


def test_a_single_spike_is_counted_not_reported() -> None:
    base = [50.0 + (i % 4) for i in range(60)]
    window = [50.0] * 10 + [500.0] + [50.0] * 10
    v = analyse(pts(base + window, T0 - timedelta(minutes=60)), T0)
    assert v.runs == [] and v.single_spikes == 1


def test_a_spike_inside_the_baseline_does_not_hide_the_anomaly() -> None:
    """Why median/MAD: with mean/stddev, this baseline spike would inflate sigma ~10x."""
    base = [100.0 + (i % 3) for i in range(60)]
    base[30] = 5000.0
    window = [130.0] * 10
    v = analyse(pts(base + window, T0 - timedelta(minutes=60)), T0)
    assert v.runs and v.runs[0].kind == "shift"


def test_flat_baseline_uses_the_floor_not_zero() -> None:
    b = baseline_of([1.0] * 30, rel_floor=0.02)
    assert b.scale == pytest.approx(0.02)  # MAD = 0 -> 2 % of the median, not a division by 0
    v = analyse(pts([1.0] * 30 + [1.01] * 10, T0 - timedelta(minutes=30)), T0)
    assert v.runs == []  # a 1 % move on a flat line is not an anomaly


def test_short_baseline_gives_no_verdict() -> None:
    v = analyse(pts([1.0] * 5 + [9.0] * 10, T0 - timedelta(minutes=5)), T0)
    assert v.baseline is None and v.reason and "baseline too short" in v.reason


# ------------------------------------------------------------------ metrics agent
async def test_metrics_agent_facts_cite_the_series_and_steady_metrics_are_facts_too() -> None:
    latency = series(lambda off: 420.0 if off >= 49 else 180.0)
    steady = series(lambda off: 250.0)
    script: dict[str, Any] = {"query_metrics": []}
    for m in METRIC_ORDER:
        if m == "p95_latency_ms":
            script["query_metrics"].append(metrics_reply(latency, m))
        elif m == "requests_per_sec":
            script["query_metrics"].append(metrics_reply(steady, m))
        else:
            script["query_metrics"].append({"_kind": "METRIC", "items": []})
    tools = FakeToolClient(script=script)
    r = await MetricsAgent(MDeps(tools=tools, budget=MetricsBudget())).run(task(), "tok", None)
    assert r.status == "SUCCEEDED"
    assert {c["tool"] for c in tools.calls} == {"query_metrics"}  # its ONE tool
    # baseline + window in one query
    assert tools.calls[0]["args"]["start"] == (T0 - timedelta(minutes=120)).isoformat()
    [a] = r.anomalies
    assert (a.metric, a.kind, a.direction) == ("p95_latency_ms", "shift", "up")
    assert a.onset == T0 + timedelta(minutes=49)
    assert any("p95_latency_ms: sustained shift up from 2026-10-02 09:49:00Z" in f.statement
               for f in r.facts)  # fmt: skip
    assert any("requests_per_sec: stayed within its baseline" in f.statement for f in r.facts)
    cited = {e.evidence_id for f in r.facts for e in f.evidence}
    assert cited == {e.evidence_key for e in r.evidence}  # every fact cites stored evidence
    assert all(e.kind == "METRIC" for e in r.evidence)
    assert any("no series for" in n for n in r.notes)  # missing metrics: notes, not facts
    assert r.trace.llm_calls == [] and r.trace.model is None


async def test_metrics_agent_stops_after_a_denial() -> None:
    tools = FakeToolClient(
        script={"query_metrics": [ToolCallError(403, "missing_permission", "no metrics:read")]}
    )
    r = await MetricsAgent(MDeps(tools=tools, budget=MetricsBudget())).run(task(), "tok", None)
    assert r.status == "FAILED" and "denied" in (r.error or "")
    assert len(tools.calls) == 1  # did not ask 17 more times for the same answer


async def test_metrics_agent_budget_cut_is_named() -> None:
    script = {"query_metrics": [{"_kind": "METRIC", "items": []} for _ in range(30)]}
    tools = FakeToolClient(script=script)
    budget = MetricsBudget(max_tool_calls=4)
    r = await MetricsAgent(MDeps(tools=tools, budget=budget)).run(
        task(services=["checkout-api", "orders-db"]), "tok", None
    )
    assert len(tools.calls) == 4
    assert r.degraded and "not checked (budget/deadline)" in r.degraded
    assert "orders-db/http_5xx_per_min" in r.degraded  # no silent false "all clear"


# ------------------------------------------------------------------ deployment agent
def deploy(
    key: str, started: datetime, keys: list[str], status: str = "SUCCEEDED"
) -> dict[str, Any]:
    return {
        "deploy_key": key,
        "service_key": "checkout-api",
        "version": "2026.10.02.4",
        "environment": "production",
        "status": status,
        "commit_sha": "ab12cd34ef56" + "0" * 28,
        "deployed_by": "cd-bot",
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(minutes=2)).isoformat(),
        "config_changed_keys": keys,
    }


async def test_deploy_facts_are_time_arithmetic_with_config_values() -> None:
    detected = T0 + timedelta(minutes=50)
    tools = FakeToolClient(
        script={
            "get_deployment": [
                {
                    "_kind": "DEPLOY",
                    "items": [
                        deploy(
                            "DEPLOY-4821",
                            T0 + timedelta(minutes=42),
                            ["feature_flags.order_batching"],
                        ),
                        deploy("DEPLOY-4700", T0 - timedelta(hours=3), []),
                    ],
                }
            ],
            "get_config_diff": [
                {
                    "_kind": "CONFIG",
                    "items": [
                        {
                            "deploy_key": "DEPLOY-4821",
                            "service_key": "checkout-api",
                            "version": "2026.10.02.4",
                            "changes": [
                                {
                                    "key": "feature_flags.order_batching",
                                    "before": False,
                                    "after": True,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    r = await DeploymentAgent(DDeps(tools=tools, budget=DeploymentBudget())).run(
        task(detected_at=detected), "tok", None
    )
    assert r.status == "SUCCEEDED"
    assert [c["tool"] for c in tools.calls] == ["get_deployment", "get_config_diff"]
    # look back 6 h: a deploy before the window can still matter
    assert tools.calls[0]["args"]["start"] == (T0 - timedelta(hours=6)).isoformat()
    by_key = {c.deploy_key: c for c in r.changes}
    assert by_key["DEPLOY-4821"].minutes_before_detection == 8.0  # 09:50 - 09:42 (start)
    assert len(by_key["DEPLOY-4821"].evidence_ids) == 2  # DEPLOY + CONFIG
    fact = next(f for f in r.facts if "DEPLOY-4821" in f.statement)
    assert "feature_flags.order_batching false -> true" in fact.statement
    assert (
        "started 8 min before the incident was detected (finished 6 min before)" in fact.statement
    )
    assert "caused" not in fact.statement.lower()  # timing, never causation (Phase 11)
    assert "no config change" in next(f for f in r.facts if "DEPLOY-4700" in f.statement).statement
    cited = {e.evidence_id for f in r.facts for e in f.evidence}
    assert cited == {e.evidence_key for e in r.evidence}


async def test_no_deploys_is_a_note_not_a_fact() -> None:
    tools = FakeToolClient(script={"get_deployment": [{"_kind": "DEPLOY", "items": []}]})
    r = await DeploymentAgent(DDeps(tools=tools, budget=DeploymentBudget())).run(task(), "t", None)
    assert r.status == "SUCCEEDED" and r.facts == []
    assert any("no production deploys" in n for n in r.notes)


async def test_deployment_agent_fails_when_no_service_answered() -> None:
    tools = FakeToolClient(
        script={"get_deployment": [ToolCallError(403, "missing_permission", "no deploys:read")]}
    )
    r = await DeploymentAgent(DDeps(tools=tools, budget=DeploymentBudget())).run(task(), "t", None)
    assert r.status == "FAILED" and "missing_permission" in (r.error or "")


# ------------------------------------------------------------------ knowledge agent
def hits(source: str, n: int = 2) -> dict[str, Any]:
    return {
        "_kind": "RUNBOOK" if source == "runbook" else "DOC",
        "items": [
            {
                "document_id": str(uuid4()),
                "chunk_id": str(uuid4()),
                "title": "SECRET TEAM PLAN: pool tuning",
                "source": source,
                "source_uri": "docs://northwind/00001",
                "content": "Restricted text that must never be stored on the incident.",
                "rank": i + 1,
            }
            for i in range(n)
        ],
    }


async def test_knowledge_stores_pointers_never_text_or_titles() -> None:
    tools = FakeToolClient(script={"search_runbooks": [hits("runbook")],
                                   "search_docs": [hits("markdown")]})  # fmt: skip
    r = await KnowledgeAgent(KDeps(tools=tools, budget=KnowledgeBudget())).run(
        task(incident_title="Checkout pool timeouts"), "tok", None
    )
    assert r.status == "SUCCEEDED"
    assert r.facts == []  # retrieval rank is not a fact about the incident
    assert {x.kind for x in r.references} == {"RUNBOOK", "DOC"}
    stored = r.model_dump_json()
    assert "SECRET TEAM PLAN" not in stored and "Restricted text" not in stored
    assert all(e.uri and e.uri.startswith("rag://documents/") for e in r.evidence)
    assert tools.calls[0]["args"]["query"] == "Checkout pool timeouts checkout-api"


async def test_knowledge_partial_failure_is_degraded_not_failed() -> None:
    tools = FakeToolClient(
        script={
            "search_runbooks": [ToolCallError(503, "dependency_unavailable", "rag down")],
            "search_docs": [hits("markdown", 1)],
        }
    )
    r = await KnowledgeAgent(KDeps(tools=tools, budget=KnowledgeBudget())).run(task(), "t", None)
    assert r.status == "SUCCEEDED" and r.degraded == "search_runbooks dependency_unavailable"


def test_query_normalises_whitespace_and_has_a_fallback() -> None:
    assert query_for(task(incident_title="  a\n\tb  ")) == "a b checkout-api"
    assert len(query_for(task(incident_title="x" * 300))) == 300


# ------------------------------------------------------------------ review findings (Phase 10)
async def _metrics_run(points: list[dict[str, Any]], metric: str) -> Any:
    script: dict[str, Any] = {"query_metrics": []}
    for m in METRIC_ORDER:
        script["query_metrics"].append(
            metrics_reply(points, m) if m == metric else {"_kind": "METRIC", "items": []}
        )
    tools = FakeToolClient(script=script)
    return await MetricsAgent(MDeps(tools=tools, budget=MetricsBudget())).run(task(), "t", None)


async def test_single_spikes_are_never_called_within_baseline() -> None:
    """H1a: a z=300 spike must not produce 'stayed within its baseline ... < 4'."""
    r = await _metrics_run(series(lambda off: 900.0 if off == 30 else 180.0), "p95_latency_ms")
    [f] = r.facts
    assert "stayed within" not in f.statement and "single-bucket spike" in f.statement


async def test_a_short_run_at_the_window_end_is_not_called_recovered() -> None:
    """H1b: two elevated buckets at the very end are 'still anomalous', not 'back within'."""
    r = await _metrics_run(series(lambda off: 420.0 if off >= 88 else 180.0), "p95_latency_ms")
    [f] = r.facts
    assert "still anomalous" in f.statement and "back within baseline" not in f.statement


async def test_a_mostly_zero_count_is_not_an_anomaly_for_a_fraction() -> None:
    """H2: median 0, MAD 0 -> the absolute floor (1 error/min) keeps 0.2/min from z=200000."""
    zero = [{"ts": p["ts"], "value": 0.0} for p in series(lambda off: 0.0)]
    for p in zero[150:153]:
        p["value"] = 0.2
    r = await _metrics_run(zero, "http_5xx_per_min")
    assert r.anomalies == []
    assert "stayed within its baseline" in r.facts[0].statement


async def test_a_rollout_spanning_detection_is_in_progress_not_after() -> None:
    """H3: started 10 min before, finished 5 min after detection -> 'still rolling out'."""
    detected = T0 + timedelta(minutes=50)
    d = deploy("DEPLOY-5000", detected - timedelta(minutes=10), [])
    d["finished_at"] = (detected + timedelta(minutes=5)).isoformat()
    tools = FakeToolClient(script={"get_deployment": [{"_kind": "DEPLOY", "items": [d]}]})
    r = await DeploymentAgent(DDeps(tools=tools, budget=DeploymentBudget())).run(
        task(detected_at=detected), "t", None
    )
    [f] = r.facts
    assert "still rolling out at detection" in f.statement and "AFTER" not in f.statement


def test_a_bucket_straddling_the_window_start_is_not_baseline() -> None:
    """L3: 5-min buckets starting 2 min before the window: that bucket holds window minutes."""
    start = T0 - timedelta(minutes=62)
    values = [100.0 + (i % 3) for i in range(12)] + [900.0] * 6  # bucket 12 starts at T0-2min
    points = [(start + timedelta(minutes=5 * i), v) for i, v in enumerate(values)]
    points[12] = (points[12][0], 900.0)
    v = analyse(points, T0, bucket=timedelta(minutes=5))
    assert v.baseline is not None and v.baseline.n == 12  # bucket 12 is not in the baseline
    assert v.baseline.median < 200
