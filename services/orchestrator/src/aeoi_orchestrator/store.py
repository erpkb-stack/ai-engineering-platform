"""Every orchestrator-schema write the graph makes, each one safe to repeat (ADR-019).

A LangGraph node can run twice: a hard kill after a node's side effect but before its
checkpoint re-runs the node on resume. So every write here is idempotent:
- tasks:      INSERT ... ON CONFLICT (idempotency_key) DO NOTHING
- executions: at most one per task (checked under a row lock on the task)
- finish:     only moves RUNNING rows; a finished investigation is never reopened
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.ids import uuid7
from aeoi_db.models.orchestrator import AgentExecution, Investigation, Message, Task
from aeoi_models.api.agents import AgentRunResult

LIVE_TASK = ("PENDING", "RUNNING")


class AlreadyRunningError(Exception):
    def __init__(self, investigation_id: UUID, requested_by: str, started_at: datetime) -> None:
        super().__init__(f"investigation {investigation_id} is RUNNING")
        self.investigation_id = investigation_id
        self.requested_by = requested_by
        self.started_at = started_at


@dataclass(frozen=True)
class Resumable:
    investigation_id: UUID
    grant_id: UUID | None
    deadline_at: datetime | None
    graph_input: dict[str, Any]


class Store:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    # ------------------------------------------------------------ investigations
    async def find_by_start_key(
        self, incident_id: UUID, requested_by: str, key: str
    ) -> Investigation | None:
        """Idempotent start: same user + same Idempotency-Key + same incident = same run."""
        async with self.sessions() as s:
            return await s.scalar(
                select(Investigation).where(
                    Investigation.incident_id == incident_id,
                    Investigation.requested_by == requested_by,
                    Investigation.plan["start_key"].astext == key,
                    # a failed start clears its key; an in-flight one has no request_id yet
                    Investigation.plan["request_id"].astext.is_not(None),
                )
            )

    async def create_investigation(
        self,
        investigation_id: UUID,
        incident_id: UUID,
        requested_by: str,
        budget_usd: Decimal,
        deadline_s: float,
        plan: dict[str, Any],
    ) -> Investigation:
        now = datetime.now(UTC)
        inv = Investigation(
            id=investigation_id,
            incident_id=incident_id,
            status="RUNNING",
            requested_by=requested_by[:120],
            budget_usd=budget_usd,
            plan=plan,
            started_at=now,
            deadline_at=now + timedelta(seconds=deadline_s),
        )
        try:
            async with self.sessions() as s, s.begin():
                s.add(inv)
        except IntegrityError as exc:
            if "uq_investigations_one_running" not in str(exc.orig):
                raise
            async with self.sessions() as s:
                other = (
                    await s.execute(
                        select(
                            Investigation.id, Investigation.requested_by, Investigation.started_at
                        ).where(
                            Investigation.incident_id == incident_id,
                            Investigation.status == "RUNNING",
                        )
                    )
                ).first()
            if other is None:  # finished between our insert and this read: let them retry
                raise
            raise AlreadyRunningError(other.id, other.requested_by, other.started_at) from exc
        return inv

    async def attach_grant(
        self,
        investigation_id: UUID,
        grant_id: UUID,
        graph_input: dict[str, Any],
        request_id: str,
    ) -> str:
        """Grant id + the graph's input (with the grant) in ONE transaction (review finding: two
        writes let a crash in between resume without a grant_id). Returns the status: anything
        but RUNNING means someone cancelled/failed it meanwhile and the caller must revoke."""
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is None:
                return "MISSING"
            if inv.status == "RUNNING":
                inv.delegation_grant_id = grant_id
                inv.plan = {**(inv.plan or {}), "input": graph_input, "request_id": request_id}
            return inv.status

    async def begin_run(self, investigation_id: UUID, deadline_s: float) -> datetime | None:
        """The deadline clock starts when the run FIRST gets a slot, not at insert (review
        finding: a queued investigation burned its deadline waiting). Resumes keep it."""
        now = datetime.now(UTC)
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is None:
                return None
            if not (inv.plan or {}).get("first_run_at"):
                inv.plan = {**(inv.plan or {}), "first_run_at": now.isoformat()}
                inv.deadline_at = now + timedelta(seconds=deadline_s)
            return inv.deadline_at

    async def note_evidence(self, investigation_id: UUID, inserted: int, error: str | None) -> None:
        """Keep the largest count: a re-run after a crash re-posts an idempotent batch and
        inserts 0 - that must not overwrite the real number (review finding)."""
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is not None:
                before = int((inv.plan or {}).get("evidence_inserted") or 0)
                inv.plan = {
                    **(inv.plan or {}),
                    "evidence_inserted": max(before, inserted),
                    "evidence_error": error,
                }

    async def get(self, investigation_id: UUID) -> Investigation | None:
        async with self.sessions() as s:
            return await s.get(Investigation, investigation_id)

    async def finish(
        self,
        investigation_id: UUID,
        status: str,
        error: str | None,
        *,
        task_status: str | None = None,
    ) -> bool:
        """RUNNING -> status (spent_usd = sum of execution costs). Live tasks get
        `task_status` (default: FAILED) so no PENDING/RUNNING task outlives its investigation.
        False = it was not RUNNING (already finished by someone else): nothing changed."""
        now = datetime.now(UTC)
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is None or inv.status != "RUNNING":
                return False
            spent = await s.scalar(
                select(func.coalesce(func.sum(AgentExecution.cost_usd), 0))
                .join(Task, Task.id == AgentExecution.task_id)
                .where(Task.investigation_id == investigation_id)
            )
            inv.status = status
            inv.finished_at = now
            inv.spent_usd = Decimal(spent or 0)
            inv.error = error[:1000] if error else None
            await s.execute(
                update(Task)
                .where(Task.investigation_id == investigation_id, Task.status.in_(LIVE_TASK))
                .values(
                    status=task_status or ("CANCELLED" if status == "CANCELLED" else "FAILED"),
                    finished_at=now,
                    error=(error or status)[:500],
                )
            )
        return True

    async def complete_after_repost(self, investigation_id: UUID) -> bool:
        """FAILED only because evidence was not stored -> COMPLETE after a successful repost.
        Any other failure stays FAILED (a repost cannot fix an agent that failed)."""
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is None or inv.status != "FAILED" or not (inv.plan or {}).get("evidence_error"):
                return False
            failed_tasks = await s.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.investigation_id == investigation_id, Task.status != "SUCCEEDED")
            )
            if failed_tasks:
                return False
            inv.status = "COMPLETE"
            inv.error = None
            inv.plan = {
                **inv.plan,
                "evidence_error": None,
                "reposted_at": datetime.now(UTC).isoformat(),
            }
            return True

    async def resumable(self) -> list[Resumable]:
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    select(
                        Investigation.id,
                        Investigation.delegation_grant_id,
                        Investigation.deadline_at,
                        Investigation.plan,
                    )
                    .where(Investigation.status == "RUNNING")
                    .order_by(Investigation.started_at)
                )
            ).all()
        return [
            Resumable(r.id, r.delegation_grant_id, r.deadline_at, (r.plan or {}).get("input", {}))
            for r in rows
        ]

    # ------------------------------------------------------------ tasks
    async def plan_tasks(self, investigation_id: UUID, tasks: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        async with self.sessions() as s, s.begin():
            for t in tasks:
                await s.execute(
                    insert(Task)
                    .values(
                        id=UUID(t["task_id"]),
                        investigation_id=investigation_id,
                        agent_name=t["agent"],
                        status="PENDING",
                        idempotency_key=t["idempotency_key"],
                        input=t["input"],
                        deadline_at=now + timedelta(seconds=float(t["deadline_s"])),
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                )

    async def start_task(self, task_id: UUID, deadline_s: float) -> int:
        """PENDING -> RUNNING (attempt 1); RUNNING again (a crash mid-call) -> attempt + 1.
        Returns the attempt number."""
        now = datetime.now(UTC)
        async with self.sessions() as s, s.begin():
            task = await s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None:
                raise LookupError(f"task {task_id} not planned")
            if task.status == "RUNNING":
                task.attempt += 1
            task.status = "RUNNING"
            task.started_at = now
            task.deadline_at = now + timedelta(seconds=deadline_s)
            return task.attempt

    async def recorded(self, task_id: UUID) -> dict[str, Any] | None:
        """The summary of an execution already recorded for this task (resume idempotency)."""
        async with self.sessions() as s:
            row = (
                await s.execute(
                    select(
                        AgentExecution.id,
                        AgentExecution.status,
                        AgentExecution.error,
                        AgentExecution.cost_usd,
                        AgentExecution.output,
                        Task.agent_name,
                    )
                    .join(Task, Task.id == AgentExecution.task_id)
                    .where(AgentExecution.task_id == task_id)
                    .order_by(AgentExecution.started_at.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        return {
            "task_id": str(task_id),
            "agent": row.agent_name,
            "status": row.status,
            "execution_id": str(row.id),
            "error": row.error,
            "cost_usd": str(row.cost_usd),
            "evidence_count": len((row.output or {}).get("evidence", [])),
            "reused": True,
        }

    async def record(
        self,
        task_id: UUID,
        agent: str,
        result: AgentRunResult | None,
        error: str | None,
        started: datetime,
        finished: datetime,
    ) -> dict[str, Any]:
        """One execution row per task, + its messages + the task's final status. If one was
        recorded meanwhile (a duplicate node run), keep it and return its summary."""
        trace = result.trace if result else None
        status = result.status if result else "FAILED"
        output: dict[str, Any] = {}
        if result is not None:
            dumped = json.loads(result.model_dump_json(exclude={"evidence"}))
            dumped["trace"].pop("messages", None)  # stored as rows in orchestrator.messages
            dumped["evidence_keys"] = [e.evidence_key for e in result.evidence]
            # the full batch, so collect_evidence (and repost) can send it after a crash
            dumped["evidence"] = [json.loads(e.model_dump_json()) for e in result.evidence]
            output = dumped
        execution_id = uuid7()
        async with self.sessions() as s, s.begin():
            await s.scalar(select(Task.id).where(Task.id == task_id).with_for_update())
            if await s.scalar(select(AgentExecution.id).where(AgentExecution.task_id == task_id)):
                existing = True
            else:
                existing = False
                s.add(
                    AgentExecution(
                        id=execution_id,
                        task_id=task_id,
                        agent_name=agent,
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
                        finished_at=finished,
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
                    .values(
                        status=status,
                        finished_at=finished,
                        error=(error or (result.error if result else None)),
                    )
                )
        if existing:
            summary = await self.recorded(task_id)
            if summary is None:  # pragma: no cover - the row was seen under the lock above
                raise LookupError(f"execution of task {task_id} vanished")
            return summary
        return {
            "task_id": str(task_id),
            "agent": agent,
            "status": status,
            "execution_id": str(execution_id),
            "error": error or (result.error if result else None),
            "cost_usd": str(trace.cost_usd if trace else Decimal(0)),
            "evidence_count": len(result.evidence) if result else 0,
            "reused": False,
        }

    async def evidence_of(self, task_id: UUID) -> list[dict[str, Any]]:
        async with self.sessions() as s:
            out = await s.scalar(
                select(AgentExecution.output).where(
                    AgentExecution.task_id == task_id, AgentExecution.status == "SUCCEEDED"
                )
            )
        return list((out or {}).get("evidence", []))

    async def note(self, investigation_id: UUID, **plan_fields: Any) -> None:
        """Merge keys into investigation.plan (graph progress notes, e.g. evidence_inserted)."""
        async with self.sessions() as s, s.begin():
            inv = await s.scalar(
                select(Investigation).where(Investigation.id == investigation_id).with_for_update()
            )
            if inv is not None:
                inv.plan = {**(inv.plan or {}), **plan_fields}

    # ------------------------------------------------------------ reads
    async def tasks(self, investigation_id: UUID) -> list[dict[str, Any]]:
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    select(
                        Task.id,
                        Task.agent_name,
                        Task.status,
                        Task.attempt,
                        Task.started_at,
                        Task.finished_at,
                        Task.error,
                        AgentExecution.model,
                        AgentExecution.latency_ms,
                        AgentExecution.cost_usd,
                        AgentExecution.output,
                    )
                    .outerjoin(AgentExecution, AgentExecution.task_id == Task.id)
                    .where(Task.investigation_id == investigation_id)
                    .order_by(Task.created_at)
                )
            ).all()
        return [
            {
                "task_id": r.id,
                "agent": r.agent_name,
                "status": r.status,
                "attempt": r.attempt,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "error": r.error,
                "model": r.model,
                "latency_ms": r.latency_ms,
                "cost_usd": r.cost_usd,
                "degraded": (r.output or {}).get("degraded"),
            }
            for r in rows
        ]

    async def list_for_incident(self, incident_id: UUID, limit: int = 20) -> list[Investigation]:
        async with self.sessions() as s:
            return list(
                (
                    await s.scalars(
                        select(Investigation)
                        .where(Investigation.incident_id == incident_id)
                        .order_by(Investigation.started_at.desc())
                        .limit(limit)
                    )
                ).all()
            )

    async def trace(self, investigation_id: UUID) -> list[dict[str, Any]]:
        async with self.sessions() as s:
            execs = (
                await s.scalars(
                    select(AgentExecution)
                    .join(Task, Task.id == AgentExecution.task_id)
                    .where(Task.investigation_id == investigation_id)
                    .order_by(AgentExecution.started_at)
                )
            ).all()
            msgs = (
                await s.scalars(
                    select(Message)
                    .where(Message.execution_id.in_([e.id for e in execs]))
                    .order_by(Message.execution_id, Message.seq)
                )
            ).all()
        by_exec: dict[UUID, list[Message]] = {}
        for m in msgs:
            by_exec.setdefault(m.execution_id, []).append(m)
        out = []
        for e in execs:
            output = dict(e.output or {})
            output.pop("evidence", None)  # evidence rows live in incident-service
            out.append(
                {
                    "execution_id": e.id,
                    "task_id": e.task_id,
                    "agent": e.agent_name,
                    "agent_version": e.agent_version,
                    "status": e.status,
                    "model": e.model,
                    "prompt_id": e.prompt_id,
                    "prompt_version": e.prompt_version,
                    "input_tokens": e.input_tokens,
                    "output_tokens": e.output_tokens,
                    "latency_ms": e.latency_ms,
                    "cost_usd": e.cost_usd,
                    "error": e.error,
                    "started_at": e.started_at,
                    "finished_at": e.finished_at,
                    "output": output,
                    "messages": [
                        {"seq": m.seq, "role": m.role, "content": m.content}
                        for m in by_exec.get(e.id, [])
                    ],
                }
            )
        return out
