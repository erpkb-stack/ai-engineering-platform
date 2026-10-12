"""Phase 16 (ADR-026): the action_executor runs exactly the approved action and reports what
the gateway said. It never retries, never changes arguments, never decides."""

from __future__ import annotations

from uuid import uuid4

from aeoi_agents.action.executor import AGENT_NAME, ActionExecutor, Deps
from aeoi_models.api.approvals import ActionTask
from aeoi_tool_client import CallContext, FakeToolClient, ToolCallError

ARGS = {"deploy_key": "DEPLOY-1042", "reason": "checkout p95 doubled after DEPLOY-1042"}


def _task() -> ActionTask:
    return ActionTask(approval_id=uuid4(), incident_id=uuid4(), action="rollback_deployment",
                      args=dict(ARGS))  # fmt: skip


class Spy(FakeToolClient):
    ctx: CallContext | None = None

    async def call(self, tool, args, ctx):  # type: ignore[no-untyped-def]
        self.ctx = ctx
        return await super().call(tool, args, ctx)


async def test_executes_the_approved_action_with_the_approval_and_incident() -> None:
    tools = Spy(script={"rollback_deployment": [{"items": [{"deploy_key": "DEPLOY-9000000001"}]}]})
    task = _task()
    res = await ActionExecutor(Deps(tools=tools)).run(task, "ic-token", "cid-1")
    assert res.status == "SUCCEEDED" and res.action is not None
    assert res.action.status == "EXECUTED" and res.action.tool_call_id is not None
    assert tools.calls == [{"tool": "rollback_deployment", "args": ARGS, "agent": AGENT_NAME}]
    assert tools.ctx is not None
    assert tools.ctx.approval_id == task.approval_id
    assert tools.ctx.incident_id == task.incident_id
    assert tools.ctx.user_token == "ic-token"  # AS THE APPROVER
    assert tools.ctx.investigation_id is None  # not part of an investigation
    assert len(res.trace.tool_calls) == 1


async def test_a_gateway_refusal_is_reported_never_retried() -> None:
    tools = FakeToolClient(script={"rollback_deployment": [
        ToolCallError(409, "approval_unusable", "This approval was already used.", str(uuid4())),
        {"items": []},  # would succeed if it retried: it must not
    ]})  # fmt: skip
    res = await ActionExecutor(Deps(tools=tools)).run(_task(), "ic-token", None)
    assert res.status == "FAILED" and res.action is not None
    assert res.action.status == "FAILED"
    assert (res.action.http_status, res.action.reason) == (409, "approval_unusable")
    assert res.action.tool_call_id is not None
    assert len(tools.calls) == 1
    assert res.error and "approval_unusable" in res.error


async def test_missing_executor_token_is_a_clear_failure() -> None:
    class NoToken(FakeToolClient):
        async def call(self, tool, args, ctx):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("secrets/action_executor_token.txt")

    res = await ActionExecutor(Deps(tools=NoToken())).run(_task(), "ic-token", None)
    assert res.status == "FAILED" and res.action is not None
    assert res.action.reason == "executor_not_configured"
    assert "make agents-tokens" in (res.action.detail or "")
