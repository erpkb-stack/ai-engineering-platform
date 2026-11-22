"""Shared plumbing for the code-only agents (Phase 10, ADR-020): one tool call, traced.

Every tool call goes through the tool gateway as the user (on-behalf-of). A call returns
the result or a Failure; the caller decides whether a failure is a note, a degraded
result or a FAILED task. Nothing here retries: the gateway already retried READ tools.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import AgentTask, ToolCallTrace
from aeoi_models.api.tools import ToolResult
from aeoi_tool_client import CallContext, ToolCallError, ToolClient


@dataclass(frozen=True)
class Failure:
    reason: str
    denied: bool


def context(task: AgentTask, agent: str, user_token: str, cid: str | None) -> CallContext:
    return CallContext(
        user_token=user_token,
        agent_name=agent,
        incident_id=task.incident_id,
        investigation_id=task.investigation_id,
        task_id=task.task_id,
        correlation_id=cid,
    )


async def call(
    tools: ToolClient, tracer: Tracer, tool: str, args: dict[str, Any], ctx: CallContext
) -> ToolResult | Failure:
    t0 = time.perf_counter()
    try:
        res = await tools.call(tool, args, ctx)
    except ToolCallError as exc:
        tracer.tool(
            ToolCallTrace(
                tool=tool,
                tool_call_id=_uuid(exc.tool_call_id),
                status="ERROR",
                reason=exc.reason,
                latency_ms=ms(t0),
            )
        )
        return Failure(exc.reason, exc.denied)
    tracer.tool(
        ToolCallTrace(
            tool=tool,
            tool_call_id=res.tool_call_id,
            status="OK",
            latency_ms=ms(t0),
            items=len(res.evidence_ids),
            truncated=res.truncated,
        )
    )
    return res


def flagged_ids(res: ToolResult) -> set[str]:
    return {f["evidence_id"] for f in res.security.injection_flags if f.get("evidence_id")}


def ts(value: Any) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError("naive datetime from a tool")
    return dt


def hhmm(dt: datetime) -> str:
    """Always UTC with a Z (Phase 8 regression: local time printed with a 'Z')."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime in a fact")
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


RATIO_SUFFIXES = ("_util", "_utilization")


def metric_value(metric: str, v: float) -> str:
    """A metric value as a reader understands it. Utilisation metrics are 0..1 ratios: the Mac
    postmortem read "DB pool utilization peaked at 1" (Phase 12 review) - shown as a percent."""
    if metric.endswith(RATIO_SUFFIXES):
        return f"{v * 100:.4g}%"
    return f"{v:.4g}"


# A model's free text is bounded in the JSON schema only against RUNAWAY output; the stored
# length is enforced HERE, by cutting at a word. A tight schema maxLength made a sentence a few
# characters too long reject the WHOLE answer (Mac, 2026-10-12: the critic's `missing` > 200
# chars twice -> no critique -> PARTIAL). Prompts still ask for short sentences.
PROSE_MAX = 1200


def clip(text: object, n: int) -> str:
    """One line, at most n characters, cut at a word boundary with an ellipsis."""
    s = " ".join(str(text or "").split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    if " " in cut[n // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


# ---------------------------------------------------------------- Phase 32a: provenance


def source_of(res: ToolResult, devdata_source: str) -> str:
    """The backend that answered a tool call. Seeded devdata keeps the agent's exact table name
    (e.g. devdata.metric_points); a real backend is named as the gateway named it (prometheus,
    loki). An older gateway that does not say = devdata."""
    src = res.source or "devdata"
    return devdata_source if src == "devdata" else src


def relabel_sources(
    facts: list[Any], evidence: list[Any], sources: dict[str, str], default: str
) -> tuple[list[Any], list[Any]]:
    """Every EvidenceRef / EvidenceItem gets the source its evidence id came from (frozen models:
    copied, never mutated). Ids not in `sources` keep `default`."""

    def ref(r: Any) -> Any:
        src = sources.get(r.evidence_id, default)
        return r if r.source_system == src else r.model_copy(update={"source_system": src})

    new_facts = [f.model_copy(update={"evidence": [ref(r) for r in f.evidence]}) for f in facts]
    new_evidence = [
        e if (src := sources.get(e.evidence_key, default)) == e.source_system
        else e.model_copy(update={"source_system": src})
        for e in evidence
    ]  # fmt: skip
    return new_facts, new_evidence
