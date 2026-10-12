"""action_executor (Phase 16, ADR-026). CODE ONLY - no model, no choice.

It runs ONE action that a human already approved, with exactly the approved arguments, AS THE
APPROVER (the IC's own token; a delegated token never acts). It does not decide anything: the
tool gateway verifies the approval at incident-service (approver, action, args hash, incident,
not expired, not used) and refuses otherwise. This agent only reports what happened.

Identity: its OWN service token (`service:action_executor`, `secrets/action_executor_token.txt`),
not the investigation workers' `service:agents`. agents.yaml trusts `agents` for read agents only,
so a leaked investigation token cannot execute anything.
[P] same process as the read agents; [E] a separate deployment with its own network policy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from aeoi_agents.common import _uuid
from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import AgentRunResult, ToolCallTrace
from aeoi_models.api.approvals import ActionExecution, ActionTask
from aeoi_tool_client import CallContext, ToolCallError, ToolClient

AGENT_NAME = "action_executor"
AGENT_VERSION = "1.0.0"


@dataclass
class Deps:
    tools: ToolClient  # MUST be built with the action_executor service token


class ActionExecutor:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def run(
        self, task: ActionTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        ctx = CallContext(user_token=user_token, agent_name=AGENT_NAME,
                          incident_id=task.incident_id, approval_id=task.approval_id,
                          task_id=task.approval_id, correlation_id=correlation_id)  # fmt: skip
        t0 = time.perf_counter()

        def done(ex: ActionExecution) -> AgentRunResult:
            tracer.tool(ToolCallTrace(
                tool=task.action, tool_call_id=ex.tool_call_id,
                status="OK" if ex.status == "EXECUTED" else "ERROR", reason=ex.reason,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            ))  # fmt: skip
            ok = ex.status == "EXECUTED"
            return AgentRunResult(
                status="SUCCEEDED" if ok else "FAILED", action=ex, trace=tracer.finish(),
                error=None if ok else f"{ex.reason}: {ex.detail}"[:500],
            )  # fmt: skip

        try:
            res = await self.deps.tools.call(task.action, dict(task.args), ctx)
        except ToolCallError as exc:
            return done(ActionExecution(
                approval_id=task.approval_id, action=task.action, status="FAILED",
                tool_call_id=_uuid(exc.tool_call_id), http_status=exc.status,
                reason=exc.reason, detail=exc.detail[:500],
            ))  # fmt: skip
        except OSError:  # the executor token file is missing: nothing was sent
            return done(ActionExecution(
                approval_id=task.approval_id, action=task.action, status="FAILED",
                http_status=503, reason="executor_not_configured",
                detail="secrets/action_executor_token.txt is missing: run make agents-tokens",
            ))  # fmt: skip
        return done(ActionExecution(
            approval_id=task.approval_id, action=task.action, status="EXECUTED",
            tool_call_id=res.tool_call_id, http_status=200, result=dict(res.data),
        ))  # fmt: skip
