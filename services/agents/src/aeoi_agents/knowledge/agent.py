"""Knowledge agent (architecture.md §10 #7): runbooks and docs for this incident. Code only.

  1 search_runbooks + search_docs (rag, hybrid retrieval) with the incident title + services
  2 POINTERS out, never text: document id, chunk id, rank, evidence id

Why pointers only (ADR-020): rag filters chunks by the USER's groups, in SQL. The result of
this agent is stored on the incident and in the trace, where anyone who may read the incident
can see it - including people outside the groups that may read that doc. Storing the text (or
even the title) would leak it. A reader opens the chunk through rag with their own token and
gets it only if their groups allow it. Choosing WHICH runbook step applies is reasoning; that
is the Phase 11 hypothesis step, which re-fetches the text as the user.

The incident title is user input; here it is only a search query (never a prompt).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aeoi_agents.common import Failure, call, context, flagged_ids
from aeoi_agents.config import KnowledgeBudget
from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import AgentRunResult, AgentTask, EvidenceItem, KnowledgeRef
from aeoi_tool_client import ToolClient

AGENT_NAME = "knowledge"
AGENT_VERSION = "1.0.0"
SOURCE = "rag.document_chunks"
DOC_SOURCES = ["markdown", "pdf", "txt", "html"]  # runbooks come from search_runbooks


@dataclass
class Deps:
    tools: ToolClient
    budget: KnowledgeBudget


def query_for(task: AgentTask) -> str:
    title = " ".join((task.incident_title or "").split())
    return f"{title} {' '.join(task.service_keys)}".strip()[:300] or "incident"


class KnowledgeAgent:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def run(
        self, task: AgentTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        b = self.deps.budget
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        ctx = context(task, AGENT_NAME, user_token, correlation_id)
        deadline = time.monotonic() + b.task_deadline_s
        query = query_for(task)
        refs: list[KnowledgeRef] = []
        evidence: list[EvidenceItem] = []
        notes: list[str] = []
        failures: list[tuple[str, Failure]] = []
        seen: set[str] = set()
        calls: list[tuple[str, dict[str, Any]]] = [
            ("search_runbooks", {"query": query, "k": b.runbooks_k}),
            ("search_docs", {"query": query, "k": b.docs_k, "sources": DOC_SOURCES}),
        ]
        for tool, args in calls:
            if time.monotonic() > deadline:
                notes.append(f"{tool}: not called (task deadline).")
                continue
            res = await call(self.deps.tools, tracer, tool, args, ctx)
            if isinstance(res, Failure):
                failures.append((tool, res))
                notes.append(f"{tool}: {res.reason}.")
                continue
            if res.data.get("degraded"):
                notes.append(f"{tool}: retrieval degraded ({str(res.data['degraded'])[:120]}).")
            hot = flagged_ids(res)
            for item in res.data.get("items", []):
                if item["chunk_id"] in seen:
                    continue
                seen.add(item["chunk_id"])
                ev_id = str(item["evidence_id"])
                kind = "RUNBOOK" if ev_id.startswith("RUNBOOK-") else "DOC"
                try:
                    ref = KnowledgeRef(
                        kind=kind,  # type: ignore[arg-type]
                        document_id=UUID(str(item["document_id"])),
                        chunk_id=UUID(str(item["chunk_id"])),
                        rank=int(item["rank"]),
                        tool=tool,
                        evidence_id=ev_id,
                        untrusted_content_flagged=ev_id in hot,
                    )
                except (ValueError, KeyError) as exc:  # a pointer we cannot resolve is useless
                    notes.append(f"{tool}: skipped an unusable hit ({type(exc).__name__}).")
                    continue
                refs.append(ref)
                evidence.append(_pointer(refs[-1], res.tool_call_id))
        if len(failures) == len(calls):
            return AgentRunResult(
                status="FAILED",
                error="no knowledge data: " + "; ".join(f"{t} {f.reason}" for t, f in failures),
                trace=tracer.finish(),
            )
        if not refs:
            notes.append("no runbook or document chunk matched (for this user's groups).")
        else:
            notes.append(
                f"{sum(r.kind == 'RUNBOOK' for r in refs)} runbook and "
                f"{sum(r.kind == 'DOC' for r in refs)} doc chunk(s) referenced; text is not "
                "stored here - open each through rag with your own permissions."
            )
        return AgentRunResult(
            status="SUCCEEDED",
            degraded="; ".join(f"{t} {f.reason}" for t, f in failures) or None,
            notes=notes,
            references=refs[:20],
            evidence=evidence[:20],
            trace=tracer.finish(),
        )


def _pointer(ref: KnowledgeRef, tool_call_id: Any) -> EvidenceItem:
    excerpt = f"rag document {ref.document_id} chunk {ref.chunk_id} (rank {ref.rank}, {ref.tool})"
    return EvidenceItem(
        evidence_key=ref.evidence_id,
        kind=ref.kind,
        source_system=SOURCE,
        title=f"{ref.kind} reference, rank {ref.rank}",
        excerpt=excerpt,
        content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        tool_call_id=tool_call_id,
        uri=f"rag://documents/{ref.document_id}#chunk={ref.chunk_id}",
    )
