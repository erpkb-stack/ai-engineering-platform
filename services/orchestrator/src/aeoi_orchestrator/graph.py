"""The investigation graph (ADR-019). Phase 9 has one agent; the shape is for many.

    START -> plan --(Send per task)--> run_agent --> collect_evidence -> finalize -> END

Checkpointed after every step (AsyncPostgresSaver, durability="sync"). On resume, finished
steps are skipped and finished Send branches keep their results; an interrupted node runs
again, so every node is idempotent (see store.py):
- plan:             deterministic task ids (uuid5 of investigation + agent), ON CONFLICT DO NOTHING
- run_agent:        returns the recorded execution if one exists; never calls the agent twice
                    for a recorded task. (A kill DURING the agent call does call it again: new
                    tool calls, task attempt + 1. That is recorded, not hidden.)
- collect_evidence: the evidence endpoint is idempotent per evidence key
- finalize:         only moves a RUNNING investigation
"""

from __future__ import annotations

import asyncio
import operator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from aeoi_models.api.agents import EvidenceItem, LogAnalysisTask
from aeoi_orchestrator.clients import AgentsClient, CallError, IncidentClient
from aeoi_orchestrator.config import Settings
from aeoi_orchestrator.delegation import DelegationClient, DelegationError
from aeoi_orchestrator.store import Store

log = structlog.get_logger(__name__)
LOG_AGENT = "log_analysis"
RETRIES = 2  # transient only: unreachable, 502/503, exchange 5xx
RETRY_BACKOFF_S = 2.0


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    incident: dict[str, Any]  # id, key, services, detected_at
    window: dict[str, str]  # start, end (ISO, UTC)
    llm_route: str | None
    llm_cache: bool
    grant_id: str
    tasks: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]  # one per task (fan-in reducer)
    evidence: dict[str, Any]
    outcome: dict[str, Any]


@dataclass(frozen=True)
class Deps:
    settings: Settings
    store: Store
    incidents: IncidentClient
    agents: AgentsClient
    delegation: DelegationClient


def task_id_for(investigation_id: str, agent: str, attempt_slot: int = 1) -> UUID:
    """Deterministic: a re-run of `plan` after a crash yields the SAME task ids."""
    return uuid5(NAMESPACE_URL, f"aeoi:investigation:{investigation_id}:{agent}:{attempt_slot}")


def build_graph(deps: Deps) -> StateGraph:  # type: ignore[type-arg]
    s = deps.settings

    async def plan(state: InvestigationState) -> dict[str, Any]:
        inv = state["investigation_id"]
        services = list(state["incident"].get("services") or [])[:3]
        tasks = [
            {
                "task_id": str(task_id_for(inv, LOG_AGENT)),
                "agent": LOG_AGENT,
                "idempotency_key": f"{inv}:{LOG_AGENT}:1",
                "deadline_s": s.task_deadline_s,
                "input": {
                    "service_keys": services,
                    "start": state["window"]["start"],
                    "end": state["window"]["end"],
                    "llm_route": state.get("llm_route"),
                    "llm_cache": state.get("llm_cache", True),
                },
            }
        ]
        # [Phase 10] metrics / deployment / code / rag / historical agents join here
        await deps.store.plan_tasks(UUID(inv), tasks)
        return {"tasks": tasks}

    def fan_out(state: InvestigationState) -> list[Send]:
        shared = {
            "investigation_id": state["investigation_id"],
            "incident": state["incident"],
            "grant_id": state["grant_id"],
        }
        return [Send("run_agent", {**shared, "task": t}) for t in state["tasks"]]

    async def run_agent(payload: dict[str, Any]) -> dict[str, Any]:
        t = payload["task"]
        task_id = UUID(t["task_id"])
        done = await deps.store.recorded(task_id)
        if done is not None:  # recorded before a crash: never call the agent twice
            log.info("task_reused_after_resume", task_id=t["task_id"])
            return {"results": [done]}
        attempt = await deps.store.start_task(task_id, s.task_deadline_s)
        started = datetime.now(UTC)
        task = LogAnalysisTask(
            incident_id=UUID(payload["incident"]["id"]),
            investigation_id=UUID(payload["investigation_id"]),
            task_id=task_id,
            service_keys=t["input"]["service_keys"],
            start=datetime.fromisoformat(t["input"]["start"]),
            end=datetime.fromisoformat(t["input"]["end"]),
            llm_route=t["input"].get("llm_route"),
            llm_cache=t["input"].get("llm_cache", True),
        )
        result = None
        error: str | None = None
        for retry in range(RETRIES + 1):
            # transient failures get a short retry here instead of becoming a recorded
            # FAILED that resume would reuse forever (review finding)
            try:
                token = await deps.delegation.token(UUID(payload["grant_id"]))
                result = await deps.agents.run_log_analysis(task, token)
                error = None
                break
            except DelegationError as exc:
                error = f"delegation refused, cannot act for the user: {exc}"
                if exc.final:
                    break
            except CallError as exc:
                error = str(exc)
                if not exc.retryable:
                    break
            except (ValueError, KeyError, TypeError) as exc:  # contract drift / bad JSON
                error = f"agent response unusable: {type(exc).__name__}: {str(exc)[:200]}"
                break
            if retry < RETRIES:
                log.warning("task_retry", task_id=t["task_id"], error=error, retry=retry + 1)
                await asyncio.sleep(RETRY_BACKOFF_S * (2**retry))
        summary = await deps.store.record(
            task_id, t["agent"], result, error, started, datetime.now(UTC)
        )
        summary["attempt"] = attempt
        return {"results": [summary]}

    async def collect_evidence(state: InvestigationState) -> dict[str, Any]:
        inv = UUID(state["investigation_id"])
        inserted, errors = 0, []
        for r in state.get("results", []):
            if r["status"] != "SUCCEEDED" or not r.get("evidence_count"):
                continue
            items = [
                EvidenceItem.model_validate(e)
                for e in await deps.store.evidence_of(UUID(r["task_id"]))
            ]
            try:
                inserted += await deps.incidents.post_evidence(
                    state["incident"]["id"],
                    inv,
                    r["agent"],
                    items,
                    f"{r['agent']}: {len(items)} evidence items",
                )
            except CallError as exc:
                # the batch stays in agent_executions.output: `repost-evidence` retries it
                errors.append(f"{r['agent']}: evidence was not stored: {exc}")
        error = "; ".join(errors) or None
        await deps.store.note_evidence(inv, inserted, error)
        return {"evidence": {"inserted": inserted, "error": error}}

    async def finalize(state: InvestigationState) -> dict[str, Any]:
        inv = UUID(state["investigation_id"])
        results = state.get("results", [])
        problems = [
            f"{r['agent']}: {r.get('error') or r['status']}"
            for r in results
            if (r["status"] != "SUCCEEDED")
        ]
        if state.get("evidence", {}).get("error"):
            problems.append(state["evidence"]["error"])
        if not results:
            problems.append("no tasks ran")
        status = "COMPLETE" if not problems else "FAILED"
        error = "; ".join(problems) or None
        # revoke BEFORE finish (review finding): a crash between them must not leave a live
        # grant behind a finished row. A crash after revoke re-runs finalize; revoke is
        # idempotent and nothing after run_agent needs a token.
        await deps.delegation.revoke(UUID(state["grant_id"]), f"investigation {status}")
        await deps.store.finish(inv, status, error)
        log.info("investigation_finished", investigation_id=str(inv), status=status)
        return {"outcome": {"status": status, "error": error}}

    g: StateGraph = StateGraph(InvestigationState)  # type: ignore[type-arg]
    g.add_node("plan", plan)
    g.add_node("run_agent", run_agent)  # type: ignore[arg-type]  # Send payload, not state
    g.add_node("collect_evidence", collect_evidence)
    g.add_node("finalize", finalize)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", fan_out, ["run_agent"])
    g.add_edge("run_agent", "collect_evidence")
    g.add_edge("collect_evidence", "finalize")
    g.add_edge("finalize", END)
    return g
