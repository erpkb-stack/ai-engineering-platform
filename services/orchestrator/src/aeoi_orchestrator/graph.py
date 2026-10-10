"""The investigation graph (ADR-019, ADR-020, ADR-021).

    START -> plan --(Send per task)--> run_agent --> collect_evidence
          -> hypothesize -> critique -> validate -> finalize -> END

Phase 11: hypothesize (code candidates, LLM ranks) and critique (LLM attacks, code merges) are
agent tasks like the evidence agents (recorded, reused on resume); validate is plain code.

Checkpointed after every step (AsyncPostgresSaver, durability="sync"). On resume, finished
steps are skipped and finished Send branches keep their results; an interrupted node runs
again, so every node is idempotent (see store.py):
- plan:             deterministic task ids (uuid5 of investigation + agent), ON CONFLICT DO NOTHING
- run_agent:        returns the recorded execution if one exists; never calls the agent twice
                    for a recorded task. (A kill DURING the agent call does call it again: new
                    tool calls, task attempt + 1. That is recorded, not hidden.)
- collect_evidence: the evidence endpoint is idempotent per evidence key
- hypothesize/critique: same reuse guard as run_agent (deterministic task ids per agent)
- validate:         the hypotheses POST is idempotent per (investigation, key)
- finalize:         only moves a RUNNING investigation

Outcome (ADR-020): every task SUCCEEDED -> COMPLETE; some failed -> PARTIAL (the evidence that
was found is kept, and the missing sources are named); none succeeded -> FAILED. A failed
agent never fails its siblings: each Send branch records its own result.
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

from aeoi_models.api.agents import AgentRunResult, AgentTask, EvidenceItem
from aeoi_models.api.hypotheses import observations_fingerprint
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_orchestrator import validation
from aeoi_orchestrator.clients import AgentsClient, CallError, IncidentClient
from aeoi_orchestrator.config import Settings
from aeoi_orchestrator.delegation import DelegationClient, DelegationError
from aeoi_orchestrator.store import Store

log = structlog.get_logger(__name__)
LOG_AGENT = "log_analysis"
RETRIES = 2  # transient only: unreachable, 502/503, exchange 5xx
RETRY_BACKOFF_S = 2.0
EVIDENCE_AGENTS = ("log_analysis", "metrics", "deployment", "knowledge")
SEVERITY = {"COMPLETE": 0, "PARTIAL": 1, "INCONCLUSIVE": 2, "FAILED": 3}


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    incident: dict[str, Any]  # id, key, services, detected_at
    window: dict[str, str]  # start, end (ISO, UTC)
    llm_route: str | None
    llm_cache: bool
    agents: list[str]  # Phase 10; absent in a Phase 9 checkpoint -> the configured set
    grant_id: str
    tasks: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]  # one per task (fan-in reducer)
    evidence: dict[str, Any]
    reasoning_enabled: bool  # Phase 11; absent in older checkpoints -> config
    reasoning: dict[str, Any]  # hypothesis / critic task summaries + validation report
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


def _task(payload: dict[str, Any], t: dict[str, Any], task_id: UUID) -> AgentTask:
    inp = t["input"]
    return AgentTask(
        incident_id=UUID(payload["incident"]["id"]),
        investigation_id=UUID(payload["investigation_id"]),
        task_id=task_id,
        service_keys=inp["service_keys"],
        start=datetime.fromisoformat(inp["start"]),
        end=datetime.fromisoformat(inp["end"]),
        llm_route=inp.get("llm_route"),
        llm_cache=inp.get("llm_cache", True),
        incident_title=(inp.get("incident_title") or "")[:300] or None,
        detected_at=datetime.fromisoformat(inp["detected_at"]) if inp.get("detected_at") else None,
    )


def outcome(results: list[dict[str, Any]], evidence_error: str | None) -> tuple[str, str | None]:
    """COMPLETE / PARTIAL / FAILED (ADR-020). Evidence that was not stored is FAILED, not
    PARTIAL: `repost-evidence` repairs it, and a repaired run then becomes COMPLETE/PARTIAL."""
    failed = [f"{r['agent']}: {r.get('error') or r['status']}" for r in results
              if r["status"] != "SUCCEEDED"]  # fmt: skip
    ok = len(results) - len(failed)
    if evidence_error:
        return "FAILED", "; ".join([*failed, evidence_error])
    if not results:
        return "FAILED", "no tasks ran"
    if ok == 0:
        return "FAILED", "; ".join(failed)
    if failed:
        return "PARTIAL", "missing sources - " + "; ".join(failed)
    return "COMPLETE", None


def reasoning_outcome(status: str, error: str | None, r: dict[str, Any]) -> tuple[str, str | None]:
    """Phase 11 on top of the evidence outcome (ADR-021). Worst status wins:
    no hypothesis survived -> INCONCLUSIVE; critic missing/unanswered or hypothesis step
    failed or not stored -> PARTIAL."""
    if not r or r.get("skipped") or status == "FAILED":
        return status, error
    problems: list[str] = []
    new = "COMPLETE"
    h = r.get("hypothesis") or {}
    v = r.get("validation") or {}
    if h.get("status") != "SUCCEEDED":
        new, problems = "PARTIAL", [f"hypothesis: {h.get('error') or h.get('status')}"]
    elif not v.get("kept", v.get("validated")):  # "validated" = the pre-fix name of "kept"
        new = "INCONCLUSIVE"
        problems.append("no hypothesis is supported by the evidence")
    else:
        if v.get("post_error"):
            new = "PARTIAL"
            problems.append(str(v["post_error"]))
        if v.get("dropped"):  # a code candidate failing a hard check means a bug or bad data
            new = "PARTIAL"
            problems.append(f"validation dropped {', '.join(v['dropped'])}")
        if v.get("ranker_degraded"):
            new = "PARTIAL"
            problems.append(f"hypothesis: {v['ranker_degraded']}")
        if "critic_answered" in (v.get("failed_checks") or []):
            new = "PARTIAL"
            c = r.get("critic") or {}
            why = v.get("critic_degraded") or c.get("error") or "gave no alternative and no reason"
            problems.append(f"critic: {why}")
        if "critic_independent" in (v.get("failed_checks") or []):
            new = "PARTIAL"
            problems.append("critic: same model as the ranker (not independent)")
    if SEVERITY[new] > SEVERITY.get(status, 0):
        status = new
    joined = "; ".join(p for p in [error, *problems] if p)
    return status, joined or None


def build_graph(deps: Deps) -> StateGraph:  # type: ignore[type-arg]
    s = deps.settings

    async def call_agent(
        agent: str, task: AgentTask | ReasoningTask, grant_id: str
    ) -> tuple[AgentRunResult | None, str | None]:
        result = None
        error: str | None = None
        for retry in range(RETRIES + 1):
            # transient failures get a short retry here instead of becoming a recorded
            # FAILED that resume would reuse forever (review finding)
            try:
                token = await deps.delegation.token(UUID(grant_id))
                result = await deps.agents.run(agent, task, token)
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
                log.warning("task_retry", agent=agent, error=error, retry=retry + 1)
                await asyncio.sleep(RETRY_BACKOFF_S * (2**retry))
        return result, error

    async def reason(
        state: InvestigationState, agent: str, build: Any
    ) -> tuple[dict[str, Any], AgentRunResult | None]:
        """One reasoning task: planned, reused if recorded, else run once (ADR-021)."""
        inv = state["investigation_id"]
        task_id = task_id_for(inv, agent)
        await deps.store.plan_tasks(UUID(inv), [{
            "task_id": str(task_id), "agent": agent, "idempotency_key": f"{inv}:{agent}:1",
            "deadline_s": s.task_deadline_s, "input": {"kind": "reasoning"},
        }])  # fmt: skip
        done = await deps.store.recorded(task_id)
        if done is None:
            attempt = await deps.store.start_task(task_id, s.task_deadline_s)
            started = datetime.now(UTC)
            task: ReasoningTask = build(task_id)
            result, error = await call_agent(agent, task, state["grant_id"])
            done = await deps.store.record(
                task_id, agent, result, error, started, datetime.now(UTC)
            )
            done["attempt"] = attempt
        out = await deps.store.output(task_id)
        return done, AgentRunResult.model_validate(out) if out else None

    async def evidence_results(state: InvestigationState) -> dict[str, AgentRunResult]:
        raw = await deps.store.succeeded_outputs(UUID(state["investigation_id"]), EVIDENCE_AGENTS)
        return {a: AgentRunResult.model_validate(o) for a, o in raw.items()}

    def reasoning_task(state: InvestigationState, results: dict[str, AgentRunResult],
                       task_id: UUID, **kw: Any) -> ReasoningTask:  # fmt: skip
        inc = state["incident"]
        return ReasoningTask(
            incident_id=UUID(inc["id"]), investigation_id=UUID(state["investigation_id"]),
            task_id=task_id, incident_title=(inc.get("title") or "")[:300] or None,
            detected_at=datetime.fromisoformat(inc["detected_at"]) if inc.get("detected_at") else None,
            results=results,  # route: the reasoning budget decides (fast = Haiku), not the log override
            llm_cache=state.get("llm_cache", True), **kw,
        )  # fmt: skip

    def enabled(state: InvestigationState) -> bool:
        return bool(state.get("reasoning_enabled", s.reasoning)) and not state.get(
            "evidence", {}
        ).get("error")

    async def hypothesize(state: InvestigationState) -> dict[str, Any]:
        if not enabled(state):
            return {"reasoning": {"skipped": True}}
        results = await evidence_results(state)
        if not results:
            return {"reasoning": {"hypothesis": {"status": "SKIPPED",
                                                 "error": "no evidence agent succeeded"}}}  # fmt: skip
        summary, _ = await reason(
            state, "hypothesis", lambda tid: reasoning_task(state, results, tid)
        )
        return {"reasoning": {"hypothesis": summary}}

    async def critique(state: InvestigationState) -> dict[str, Any]:
        r = dict(state.get("reasoning") or {})
        if r.get("skipped") or (r.get("hypothesis") or {}).get("status") != "SUCCEEDED":
            return {}
        inv = state["investigation_id"]
        hyp = await deps.store.output(task_id_for(inv, "hypothesis"))
        hres = AgentRunResult.model_validate(hyp) if hyp else None
        ranked = hres.hypotheses if hres else []
        if not ranked:
            return {"reasoning": {**r, "critic": {"status": "SKIPPED", "error": "no candidate"}}}
        results = await evidence_results(state)
        # independence: the critic gets code statements + refs, never the ranker's prose/rank
        blind = [h.model_copy(update={"explanation": "", "explanation_refs": [],
                                      "rank": i + 1, "ranked_by": "rubric"})
                 for i, h in enumerate(sorted(ranked, key=lambda h: h.key))]  # fmt: skip
        summary, _ = await reason(
            state, "critic",
            lambda tid: reasoning_task(
                state, results, tid, hypotheses=blind,
                observations_sha=observations_fingerprint(hres.observations) if hres else None,
            ),
        )  # fmt: skip
        return {"reasoning": {**r, "critic": summary}}

    async def validate(state: InvestigationState) -> dict[str, Any]:
        r = dict(state.get("reasoning") or {})
        if r.get("skipped") or (r.get("hypothesis") or {}).get("status") != "SUCCEEDED":
            return {}
        inv = state["investigation_id"]
        hyp_raw = await deps.store.output(task_id_for(inv, "hypothesis"))
        hyp = AgentRunResult.model_validate(hyp_raw) if hyp_raw else None
        crit_raw = await deps.store.output(task_id_for(inv, "critic"))
        crit = AgentRunResult.model_validate(crit_raw) if crit_raw else None
        if hyp is None:
            return {}
        results = await evidence_results(state)
        stored = {e.evidence_key for res in results.values() for e in res.evidence}
        final = validation.merge(hyp.hypotheses, crit)
        kept, report = validation.validate(
            final, hyp.observations, stored, crit, ranker_model=hyp.trace.model
        )
        batch = validation.to_batch(inv, kept, hyp.ruled_out, hyp.observations)
        post_error = None
        if batch is not None:
            try:
                await deps.incidents.post_hypotheses(state["incident"]["id"], batch)
            except CallError as exc:
                post_error = f"hypotheses were not stored: {exc}"
        summary = {
            # kept = survived the hard checks; validated = status VALIDATED (the Mac run printed
            # "validated: 3" for three CHALLENGED/PROPOSED hypotheses - a misleading name)
            "kept": len(kept),
            "validated": sum(1 for h in kept if h.status == "VALIDATED"),
            "dropped": report.dropped,
            "ruled_out": len(hyp.ruled_out),
            "passed": report.passed,
            "failed_checks": [c.name for c in report.checks if not c.passed],
            "top": kept[0].statement[:200] if kept else None,
            "post_error": post_error,
            "ranker_degraded": hyp.degraded,
            "critic_degraded": crit.degraded if crit is not None else None,
        }
        await deps.store.note(UUID(inv), hypotheses=summary)
        return {"reasoning": {**r, "validation": summary}}

    async def plan(state: InvestigationState) -> dict[str, Any]:
        inv = state["investigation_id"]
        incident = state["incident"]
        services = list(incident.get("services") or [])[:3]
        # The plan is CODE (ADR-020): every configured agent runs. An LLM planner choosing
        # among 4 cheap, read-only agents would cost a model call to save a few tool calls.
        tasks = [
            {
                "task_id": str(task_id_for(inv, agent)),
                "agent": agent,
                "idempotency_key": f"{inv}:{agent}:1",
                "deadline_s": s.task_deadline_s,
                "input": {
                    "service_keys": services,
                    "start": state["window"]["start"],
                    "end": state["window"]["end"],
                    "llm_route": state.get("llm_route"),
                    "llm_cache": state.get("llm_cache", True),
                    "incident_title": incident.get("title"),
                    "detected_at": incident.get("detected_at"),
                },
            }
            for agent in (state.get("agents") or s.agents)
        ]
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
        try:
            task = _task(payload, t, task_id)
        except (ValueError, KeyError, TypeError) as exc:
            # a bad task must fail ITS branch, not the investigation (review finding)
            summary = await deps.store.record(
                task_id, t["agent"], None, f"invalid task: {str(exc)[:300]}", started,
                datetime.now(UTC),
            )  # fmt: skip
            summary["attempt"] = attempt
            return {"results": [summary]}
        result, error = await call_agent(t["agent"], task, payload["grant_id"])
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
        status, error = outcome(results, state.get("evidence", {}).get("error"))
        status, error = reasoning_outcome(status, error, state.get("reasoning") or {})
        # revoke BEFORE finish (review finding): a crash between them must not leave a live
        # grant behind a finished row. A crash after revoke re-runs finalize; revoke is
        # idempotent and nothing after critique needs a token.
        await deps.delegation.revoke(UUID(state["grant_id"]), f"investigation {status}")
        await deps.store.finish(inv, status, error)
        log.info("investigation_finished", investigation_id=str(inv), status=status)
        return {"outcome": {"status": status, "error": error}}

    g: StateGraph = StateGraph(InvestigationState)  # type: ignore[type-arg]
    g.add_node("plan", plan)
    g.add_node("run_agent", run_agent)  # type: ignore[arg-type]  # Send payload, not state
    g.add_node("collect_evidence", collect_evidence)
    g.add_node("hypothesize", hypothesize)
    g.add_node("critique", critique)
    g.add_node("validate", validate)
    g.add_node("finalize", finalize)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", fan_out, ["run_agent"])
    g.add_edge("run_agent", "collect_evidence")
    g.add_edge("collect_evidence", "hypothesize")
    g.add_edge("hypothesize", "critique")
    g.add_edge("critique", "validate")
    g.add_edge("validate", "finalize")
    g.add_edge("finalize", END)
    return g
