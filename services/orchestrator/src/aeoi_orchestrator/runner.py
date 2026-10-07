"""Phase 8 thin runner: ONE agent task, recorded end to end. ADR-018.

  1 read the incident AS THE USER (incident-service checks incidents:read)
  2 T1: investigation RUNNING + task RUNNING          (orchestrator schema, orch_svc login)
  3 call the agent (service token agents:run + the user's token as on-behalf-of)
  4 T2: agent_execution + messages + task status       (the "trace row")
  5 POST cited evidence to incident-service (idempotent; scope evidence:write)
  6 T3: investigation COMPLETE / FAILED + spent_usd

Steps 4-6 span two services, so they can't be one transaction. Recovery, per failure:
- agent error / bad response / evidence POST failure -> recorded FAILED with the reason; the
  evidence batch is kept in agent_executions.output, `repost-evidence` retries it (idempotent).
  A re-run is NOT a retry: it makes new tool calls with new evidence ids.
- exception or Ctrl-C inside the runner -> task + investigation marked FAILED (_abort).
- hard kill (no finally) -> the next run on that incident cancels RUNNING investigations older
  than 2 x the task deadline and marks their tasks TIMED_OUT.
[Phase 9] LangGraph + checkpoints replace this file; the recorded rows stay the same.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.correlation import CORRELATION_HEADER, get_correlation_id, new_correlation_id
from aeoi_common.ids import uuid7
from aeoi_db.models.orchestrator import AgentExecution, Investigation, Message, Task
from aeoi_models.api.agents import (
    AgentRunResult,
    EvidenceBatch,
    EvidenceItem,
    LlmRoute,
    LogAnalysisTask,
)
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER
from aeoi_orchestrator.config import Settings

log = structlog.get_logger(__name__)
AGENT = "log_analysis"


class RunnerError(Exception):
    pass


@dataclass
class RunSummary:
    investigation_id: UUID
    task_id: UUID
    execution_id: UUID | None
    incident_key: str
    status: str
    result: AgentRunResult | None = None
    evidence_inserted: int = 0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Runner:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        service_token: str,
    ) -> None:
        self.s = settings
        self.sessions = sessions
        self.http = http
        self.token = service_token

    async def run_log_analysis(
        self,
        incident_ref: str,
        user_token: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        llm_route: LlmRoute | None = None,
        llm_cache: bool = True,
        requested_by: str = "user:cli",
    ) -> RunSummary:
        cid = get_correlation_id() or new_correlation_id()
        incident = await self._incident(incident_ref, user_token, cid)
        services = list(incident.get("affected_services") or [])[:3]
        if not services:
            raise RunnerError(f"{incident['key']} has no affected_services: nothing to search")
        detected = datetime.fromisoformat(incident["detected_at"])
        start = start or detected - timedelta(minutes=self.s.window_before_min)
        end = end or detected + timedelta(minutes=self.s.window_after_min)

        investigation_id, task_id = uuid7(), uuid7()
        task = LogAnalysisTask(
            incident_id=UUID(incident["id"]),
            investigation_id=investigation_id,
            task_id=task_id,
            service_keys=services,
            start=start,
            end=end,
            llm_route=llm_route,
            llm_cache=llm_cache,
        )
        await self._open(investigation_id, task_id, task, requested_by)
        summary = RunSummary(investigation_id, task_id, None, incident["key"], "RUNNING")
        result: AgentRunResult | None = None
        error: str | None = None
        started = datetime.now(UTC)
        try:
            try:
                result = await self._call_agent(task, user_token, cid)
            except RunnerError as exc:
                error = str(exc)
            except Exception as exc:  # bad JSON, contract drift: still a recorded FAILURE
                error = f"agent response unusable: {type(exc).__name__}: {str(exc)[:200]}"
            finished = datetime.now(UTC)
            summary.result = result
            summary.execution_id = await self._record(task_id, result, error, started, finished)
            if result is not None and result.status == "SUCCEEDED" and result.evidence:
                try:
                    summary.evidence_inserted = await self.post_evidence(
                        incident["id"], investigation_id, result.evidence, result, cid
                    )
                except RunnerError as exc:
                    # the batch is kept in agent_executions.output["evidence"]:
                    # `repost-evidence <investigation>` retries it (a re-run would NOT: it makes
                    # new tool calls with new evidence ids - review finding)
                    error = f"agent succeeded but evidence was not stored: {exc}"
        except BaseException as exc:
            # DB error while recording, Ctrl-C, cancellation: never leave RUNNING rows behind
            error = error or f"runner interrupted: {type(exc).__name__}"
            await self._abort(investigation_id, task_id, error)
            raise
        ok = result is not None and result.status == "SUCCEEDED" and error is None
        summary.status = "COMPLETE" if ok else "FAILED"
        summary.error = error or (result.error if result else None)
        cost = result.trace.cost_usd if result else Decimal(0)
        await self._close(investigation_id, summary.status, cost, summary.error)
        log.info(
            "investigation_finished",
            investigation_id=str(investigation_id),
            status=summary.status,
            incident=incident["key"],
        )
        return summary

    # ------------------------------------------------------------------ steps
    async def _incident(self, ref: str, user_token: str, cid: str) -> dict[str, Any]:
        r = await self.http.get(
            f"{self.s.incident_service_url}/v1/incidents/{ref}",
            headers={"Authorization": f"Bearer {user_token}", CORRELATION_HEADER: cid},
        )
        if r.status_code != 200:
            raise RunnerError(f"cannot read incident {ref}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()  # type: ignore[no-any-return]

    async def _open(
        self, investigation_id: UUID, task_id: UUID, task: LogAnalysisTask, requested_by: str
    ) -> None:
        now = datetime.now(UTC)
        stale = now - timedelta(seconds=2 * self.s.task_deadline_s)
        async with self.sessions() as s, s.begin():
            # self-heal after a hard crash (kill -9): a RUNNING investigation older than
            # 2 x deadline is dead - cancel it AND its tasks (review finding: tasks stayed RUNNING)
            dead = (
                await s.scalars(
                    update(Investigation)
                    .where(
                        Investigation.incident_id == task.incident_id,
                        Investigation.status == "RUNNING",
                        Investigation.started_at < stale,
                    )
                    .values(status="CANCELLED", finished_at=now)
                    .returning(Investigation.id)
                )
            ).all()
            if dead:
                await s.execute(
                    update(Task)
                    .where(Task.investigation_id.in_(dead), Task.status.in_(("PENDING", "RUNNING")))
                    .values(status="TIMED_OUT", finished_at=now, error="investigation went stale")
                )
        try:
            async with self.sessions() as s, s.begin():
                s.add(
                    Investigation(
                        id=investigation_id,
                        incident_id=task.incident_id,
                        status="RUNNING",
                        requested_by=requested_by[:120],
                        budget_usd=self.s.budget_usd,
                        plan={
                            "phase": 8,
                            "agents": [AGENT],
                            "services": task.service_keys,
                            "window": [task.start.isoformat(), task.end.isoformat()],
                            "llm_route": task.llm_route,
                        },
                    )
                )
                await s.flush()
                s.add(
                    Task(
                        id=task_id,
                        investigation_id=investigation_id,
                        agent_name=AGENT,
                        status="RUNNING",
                        idempotency_key=f"{investigation_id}:{AGENT}:1",
                        input=json.loads(task.model_dump_json()),
                        deadline_at=now + timedelta(seconds=self.s.task_deadline_s),
                        started_at=now,
                    )
                )
        except IntegrityError as exc:
            if "uq_investigations_one_running" not in str(exc.orig):
                raise
            async with self.sessions() as s:
                other = (
                    await s.execute(
                        select(
                            Investigation.id, Investigation.requested_by, Investigation.started_at
                        ).where(
                            Investigation.incident_id == task.incident_id,
                            Investigation.status == "RUNNING",
                        )
                    )
                ).first()
            who = (
                f" {other.id} (by {other.requested_by}, started "
                f"{int((datetime.now(UTC) - other.started_at).total_seconds())}s ago)"
                if other
                else ""
            )
            raise RunnerError(
                f"another investigation of this incident is RUNNING{who}. One at a time per "
                "incident (no duplicate work). Wait for it - is another terminal running "
                f"agent-run/compare? A crashed one is cancelled after {2 * self.s.task_deadline_s:.0f}s."
            ) from exc

    async def _call_agent(self, task: LogAnalysisTask, user_token: str, cid: str) -> AgentRunResult:
        try:
            r = await self.http.post(
                f"{self.s.agents_url}/v1/agents/{AGENT}/run",
                content=task.model_dump_json(),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    ON_BEHALF_OF_HEADER: f"Bearer {user_token}",
                    CORRELATION_HEADER: cid,
                    "Content-Type": "application/json",
                },
                timeout=self.s.task_deadline_s,
            )
        except httpx.TimeoutException as exc:
            raise RunnerError(f"agent timed out after {self.s.task_deadline_s:.0f}s") from exc
        except httpx.TransportError as exc:
            raise RunnerError(f"agents service unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise RunnerError(f"agent returned HTTP {r.status_code}: {r.text[:300]}")
        return AgentRunResult.model_validate(r.json())

    async def _record(
        self,
        task_id: UUID,
        result: AgentRunResult | None,
        error: str | None,
        started: datetime,
        finished: datetime,
    ) -> UUID:
        """Times are the CALL's start/end measured here (DB CHECK finished >= started); a
        server default of now() at insert time would claim the agent ran after it finished."""
        execution_id = uuid7()
        now = finished
        trace = result.trace if result else None
        status = result.status if result else "FAILED"
        output: dict[str, Any] = {}
        if result is not None:
            dumped = json.loads(result.model_dump_json(exclude={"evidence"}))
            dumped["trace"].pop("messages", None)  # stored as rows in orchestrator.messages
            dumped["evidence_keys"] = [e.evidence_key for e in result.evidence]
            # full batch kept so a failed evidence POST can be retried later (repost-evidence)
            dumped["evidence"] = [json.loads(e.model_dump_json()) for e in result.evidence]
            output = dumped
        async with self.sessions() as s, s.begin():
            s.add(
                AgentExecution(
                    id=execution_id,
                    task_id=task_id,
                    agent_name=AGENT,
                    agent_version=trace.agent_version if trace else "unknown",
                    model=trace.model if trace else None,
                    prompt_id=trace.prompt_id if trace else None,
                    prompt_version=trace.prompt_version if trace else None,
                    status=status,
                    input_tokens=trace.input_tokens if trace else 0,
                    output_tokens=trace.output_tokens if trace else 0,
                    latency_ms=trace.latency_ms if trace else None,
                    cost_usd=trace.cost_usd if trace else Decimal(0),
                    retries=trace.retries if trace else 0,
                    error=(error or (result.error if result else None)),
                    output=output,
                    started_at=started,
                    finished_at=now,
                )
            )
            await s.flush()
            for seq, m in enumerate(trace.messages if trace else [], start=1):
                s.add(
                    Message(
                        id=uuid7(),
                        execution_id=execution_id,
                        seq=seq,
                        role=m.role,
                        content=m.content,
                        token_count=None,
                    )
                )
            await s.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(status=status, finished_at=now, error=error)
            )
        return execution_id

    async def post_evidence(
        self,
        incident_id: str,
        investigation_id: UUID | None,
        items: list[EvidenceItem],
        result: AgentRunResult | None,
        cid: str,
    ) -> int:
        n_facts = len(result.facts) if result else 0
        n_clusters = len(result.clusters) if result else 0
        degraded = result.degraded if result else None
        batch = EvidenceBatch(
            agent=AGENT,
            investigation_id=investigation_id,
            items=items[:200],
            summary=(
                f"Log analysis: {n_facts} facts, {n_clusters} clusters"
                + (f" (degraded: {degraded})" if degraded else "")
            )[:300],
        )
        last = ""
        for _ in range(2):  # idempotent endpoint: a retry is safe
            try:
                r = await self.http.post(
                    f"{self.s.incident_service_url}/v1/incidents/{incident_id}/evidence",
                    content=batch.model_dump_json(),
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        CORRELATION_HEADER: cid,
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TransportError as exc:
                last = type(exc).__name__
                continue
            if r.status_code == 200:
                try:
                    return int(r.json()["inserted"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise RunnerError(f"unexpected evidence response: {r.text[:200]}") from exc
            last = f"HTTP {r.status_code} {r.text[:200]}"
            if r.status_code < 500:
                break
        raise RunnerError(last)

    async def status(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Investigations of one incident with their tasks and execution summary (dev aid)."""
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    select(
                        Investigation.id,
                        Investigation.status,
                        Investigation.requested_by,
                        Investigation.started_at,
                        Investigation.finished_at,
                        Investigation.plan,
                        Task.status.label("task"),
                        AgentExecution.model,
                        AgentExecution.latency_ms,
                        AgentExecution.output,
                    )
                    .outerjoin(Task, Task.investigation_id == Investigation.id)
                    .outerjoin(AgentExecution, AgentExecution.task_id == Task.id)
                    .where(Investigation.incident_id == incident_id)
                    .order_by(Investigation.started_at)
                )
            ).all()
        return [
            {
                "investigation": str(r.id),
                "status": r.status,
                "task": r.task,
                "requested_by": r.requested_by,
                "started_at": r.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
                "seconds": int((r.finished_at - r.started_at).total_seconds())
                if r.finished_at
                else None,
                "route": (r.plan or {}).get("llm_route"),
                "model": r.model,
                "degraded": (r.output or {}).get("degraded"),
                "error": (r.plan or {}).get("error"),
            }
            for r in rows
        ]

    async def incident_id(self, ref: str, user_token: str) -> UUID:
        return UUID((await self._incident(ref, user_token, new_correlation_id()))["id"])

    async def repost_evidence(self, investigation_id: UUID) -> int:
        """Retry the evidence POST of a finished investigation from its recorded batch."""
        async with self.sessions() as s:
            row = (
                await s.execute(
                    select(Investigation.incident_id, AgentExecution.output)
                    .join(Task, Task.investigation_id == Investigation.id)
                    .join(AgentExecution, AgentExecution.task_id == Task.id)
                    .where(
                        Investigation.id == investigation_id, AgentExecution.status == "SUCCEEDED"
                    )
                )
            ).first()
        if row is None:
            raise RunnerError("no succeeded execution for that investigation")
        items = [EvidenceItem.model_validate(e) for e in row.output.get("evidence", [])]
        if not items:
            return 0
        inserted = await self.post_evidence(
            str(row.incident_id), investigation_id, items, None, new_correlation_id()
        )
        await self._close(investigation_id, "COMPLETE", Decimal(0), None, reopen=True)
        return inserted

    async def _abort(self, investigation_id: UUID, task_id: UUID, error: str) -> None:
        now = datetime.now(UTC)
        try:
            async with self.sessions() as s, s.begin():
                await s.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status.in_(("PENDING", "RUNNING")))
                    .values(status="FAILED", finished_at=now, error=error[:500])
                )
                await s.execute(
                    update(Investigation)
                    .where(Investigation.id == investigation_id, Investigation.status == "RUNNING")
                    .values(status="FAILED", finished_at=now)
                )
        except Exception:  # the DB itself may be what failed; the stale-heal covers that case
            log.exception("investigation_abort_failed", investigation_id=str(investigation_id))

    async def _close(
        self,
        investigation_id: UUID,
        status: str,
        cost: Decimal,
        error: str | None,
        *,
        reopen: bool = False,
    ) -> None:
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is None:
                return
            if reopen and inv.status != "FAILED":
                return  # only a FAILED investigation can be completed by a repost
            inv.status = status
            inv.spent_usd = (inv.spent_usd or Decimal(0)) + cost
            inv.finished_at = datetime.now(UTC)
            if error:
                inv.plan = {**inv.plan, "error": error[:500]}
