"""Runs graphs in the background, resumes them after a restart, cancels them (ADR-019).

[P]    in-process asyncio tasks, one orchestrator replica.
[Prod] a worker pool with a lease per investigation (SELECT ... FOR UPDATE SKIP LOCKED) or
       Kafka partition ownership (Phase 18). Two replicas running this code would both resume
       the same RUNNING investigation - the one-RUNNING index does not prevent that.

Outcomes, by cause:
- graph finished          -> finalize wrote COMPLETE/PARTIAL/FAILED and revoked the grant
- deadline exceeded       -> live tasks TIMED_OUT, grant revoked; the FINISHED tasks'
                             evidence is posted (salvage) -> PARTIAL, or FAILED if none
                             finished (review finding: one slow agent discarded the rest)
- user cancel             -> CANCELLED, live tasks CANCELLED, grant revoked
- process shutdown        -> rows stay RUNNING ON PURPOSE: the next start resumes them
- unexpected exception    -> FAILED with the error, grant revoked (if the DB itself failed,
                             the row stays RUNNING and the next start retries it)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from aeoi_orchestrator.clients import IncidentClient
from aeoi_orchestrator.delegation import DelegationClient
from aeoi_orchestrator.salvage import post_recorded_evidence
from aeoi_orchestrator.store import Store

log = structlog.get_logger(__name__)


class Engine:
    def __init__(
        self,
        graph: CompiledStateGraph,  # type: ignore[type-arg]
        store: Store,
        delegation: DelegationClient,
        max_concurrent: int,
        deadline_s: float,
        incidents: IncidentClient | None = None,
    ) -> None:
        self.graph = graph
        self.incidents = incidents
        self.store = store
        self.delegation = delegation
        self.deadline_s = deadline_s
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._cancelling: set[UUID] = set()

    @staticmethod
    def _config(investigation_id: UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": str(investigation_id)}}

    def running(self, investigation_id: UUID) -> bool:
        t = self._tasks.get(investigation_id)
        return t is not None and not t.done()

    def launch(self, investigation_id: UUID, graph_input: dict[str, Any]) -> None:
        if self.running(investigation_id):
            return
        task = asyncio.create_task(
            self._run(investigation_id, graph_input), name=f"investigation-{investigation_id}"
        )
        self._tasks[investigation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(investigation_id, None))

    async def wait(self, investigation_id: UUID) -> None:
        """Tests/CLI aid: wait for a launched investigation to stop."""
        t = self._tasks.get(investigation_id)
        if t is not None:
            await asyncio.gather(t, return_exceptions=True)

    async def next_nodes(self, investigation_id: UUID) -> list[str]:
        snap = await self.graph.aget_state(self._config(investigation_id))
        return list(snap.next or ())

    async def _fail(
        self, investigation_id: UUID, error: str, grant_id: UUID | None, *, task_status: str
    ) -> None:
        # revoke FIRST (review finding): a crash between finish and revoke left a live grant
        # for a finished investigation, and nothing would ever revoke it
        if grant_id is not None:
            await self.delegation.revoke(grant_id, f"investigation FAILED: {error[:120]}")
        await self.store.finish(investigation_id, "FAILED", error, task_status=task_status)

    async def _deadline(self, investigation_id: UUID, grant_id: UUID | None) -> None:
        """Keep what finished: post the evidence of SUCCEEDED tasks, then PARTIAL (or FAILED
        if nothing finished, or the evidence could not be stored - repost-evidence repairs)."""
        reason = "investigation deadline exceeded"
        inv = await self.store.get(investigation_id)
        succeeded, error = 0, None
        if inv is not None and self.incidents is not None:
            succeeded, inserted, error = await post_recorded_evidence(
                self.store, self.incidents, investigation_id, inv.incident_id, "salvaged"
            )
            await self.store.note_evidence(investigation_id, inserted, error)
        if succeeded == 0 or error:
            await self._fail(
                investigation_id, reason + (f"; {error}" if error else ""), grant_id,
                task_status="TIMED_OUT",
            )  # fmt: skip
            return
        live = [t["agent"] for t in await self.store.tasks(investigation_id)
                if t["status"] != "SUCCEEDED"]  # fmt: skip
        if grant_id is not None:
            await self.delegation.revoke(grant_id, "investigation PARTIAL: deadline")
        await self.store.finish(
            investigation_id,
            "PARTIAL",
            f"{reason} - missing sources: " + ", ".join(f"{a}: TIMED_OUT" for a in live),
            task_status="TIMED_OUT",
        )

    async def _run(self, investigation_id: UUID, graph_input: dict[str, Any]) -> None:
        async with self._sem:
            inv = await self.store.get(investigation_id)
            if inv is None or inv.status != "RUNNING":
                return
            grant = inv.delegation_grant_id
            deadline_at = await self.store.begin_run(investigation_id, self.deadline_s)
            remaining = (deadline_at - datetime.now(UTC)).total_seconds() if deadline_at else 0
            if remaining <= 0:
                await self._fail(
                    investigation_id,
                    "investigation deadline exceeded",
                    grant,
                    task_status="TIMED_OUT",
                )
                return
            cfg = self._config(investigation_id)
            snap = await self.graph.aget_state(cfg)
            # no checkpoint yet (crash right after start) -> start from the stored input
            arg = None if snap.values else graph_input
            log.info(
                "investigation_run",
                investigation_id=str(investigation_id),
                resume=arg is None,
                next=list(snap.next or ()),
            )
            try:
                await asyncio.wait_for(
                    self.graph.ainvoke(arg, cfg, durability="sync"), timeout=remaining
                )
            except TimeoutError:
                await self._deadline(investigation_id, grant)
            except asyncio.CancelledError:
                if investigation_id not in self._cancelling:
                    log.info(
                        "investigation_paused_for_shutdown", investigation_id=str(investigation_id)
                    )
                raise
            except Exception as exc:
                log.exception("investigation_crashed", investigation_id=str(investigation_id))
                try:
                    await self._fail(
                        investigation_id,
                        f"orchestrator error: {type(exc).__name__}: {str(exc)[:300]}",
                        grant,
                        task_status="FAILED",
                    )
                except Exception:  # DB down too: leave RUNNING, the next start resumes
                    log.exception("investigation_fail_record_failed")

    async def resume_pending(self, only: UUID | None = None) -> list[UUID]:
        """Startup: relaunch every RUNNING investigation this process is not running
        (`only` = just that one: the operator's resume endpoint)."""
        resumed: list[UUID] = []
        for r in await self.store.resumable():
            if only is not None and r.investigation_id != only:
                continue
            if self.running(r.investigation_id):
                continue
            if r.grant_id is None:
                # crashed between insert and token exchange: nobody can act for the user
                await self.store.finish(
                    r.investigation_id,
                    "FAILED",
                    "orchestrator stopped before the delegation was created",
                )
                continue
            if r.deadline_at is None or r.deadline_at <= datetime.now(UTC):
                await self._fail(
                    r.investigation_id,
                    "investigation deadline passed while the orchestrator was down",
                    r.grant_id,
                    task_status="TIMED_OUT",
                )
                continue
            # the grant column is the truth (review finding: input could lack it)
            self.launch(r.investigation_id, {**r.graph_input, "grant_id": str(r.grant_id)})
            resumed.append(r.investigation_id)
        if resumed:
            log.info("investigations_resumed", count=len(resumed))
        return resumed

    async def cancel(self, investigation_id: UUID, reason: str) -> bool:
        self._cancelling.add(investigation_id)
        try:
            t = self._tasks.get(investigation_id)
            if t is not None and not t.done():
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
            inv = await self.store.get(investigation_id)
            # revoke whatever happened above: a cancelled finalize may have lost its own revoke
            # (review finding); the STS revoke is idempotent
            if inv is not None and inv.delegation_grant_id is not None:
                await self.delegation.revoke(inv.delegation_grant_id, f"cancelled: {reason}")
            return await self.store.finish(investigation_id, "CANCELLED", reason)
        finally:
            self._cancelling.discard(investigation_id)

    async def shutdown(self) -> None:
        """Stop running graphs WITHOUT finishing their rows: they resume on the next start."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
