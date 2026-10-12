"""Phase 16 (ADR-026): decide, then act - in the approver's own request.

POST /v1/approvals/{id}/decision   the IC decides (incident-service enforces every rule:
                                   role, not the requester, not decided, not expired). If
                                   APPROVED, the action runs NOW, AS THE IC: the
                                   action_executor agent -> tool gateway (verifies + consumes
                                   the approval) -> simulated CD. The outcome is recorded.
POST /v1/approvals/{id}/execute    retry of an approved action whose result is unknown
                                   (agents or gateway did not answer). Safe: the gateway runs
                                   one approval at most once, the CD record is keyed by it.

Why synchronous and not a LangGraph interrupt (ADR-007 said "interrupt"): an interrupt keeps
the investigation and its delegated token alive while a human thinks (minutes to hours); a
short-lived delegated token could not act anyway (DELEGATED_CANNOT_ACT). Here nothing waits:
the investigation ends with a pending request; the decision brings its own credential.

The IC's token is used inside this request only, never stored (same rule as a start).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request

from aeoi_common.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamUnavailableError,
    ValidationFailedError,
)
from aeoi_models.api.approvals import (
    ActionTask,
    ApprovalOut,
    DecisionIn,
    DecisionResult,
    OutcomeIn,
)
from aeoi_orchestrator.clients import AgentsClient, CallError, IncidentClient
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web import require

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["approvals"])
Approver = Annotated[Principal, Depends(require(Perm.ACTIONS_APPROVE))]
EXECUTOR = "action_executor"
# the gateway already ran this approval: the action happened, only its record was missing
ALREADY_EXECUTED = "approval_already_executed"
# the gateway's reason when the TOOL ran and failed (e.g. 409 a newer deploy exists). Only this,
# with a 4xx, is a definite FAILED. A refused approval (unusable / invalid / in progress /
# executor_only ...) is a refused ATTEMPT, not a failed action (review H2): never recorded.
TOOL_FAILED = "execution_failed"


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _raise(exc: CallError) -> None:
    """incident-service's answer, passed through with its meaning."""
    msg = str(exc)
    if exc.status == 403:
        raise ForbiddenError(msg) from exc
    if exc.status == 404:
        raise NotFoundError(msg) from exc
    if exc.status == 409:
        raise ConflictError(msg) from exc
    if exc.status == 422:
        raise ValidationFailedError(msg) from exc
    raise UpstreamUnavailableError(f"incident-service: {msg}") from exc


def _retry_hint(approval_id: UUID) -> str:
    return (f"Retry with POST /api/v1/approvals/{approval_id}/execute - safe: one approval "
            "never runs twice.")  # fmt: skip


async def execute(request: Request, a: ApprovalOut, user_token: str) -> DecisionResult:
    """Run one APPROVED action as the approver and record what happened. Records FAILED only
    when the failure is certain (a 4xx answer); an unknown result is reported, not recorded."""
    incidents: IncidentClient = request.app.state.incidents
    agents: AgentsClient = request.app.state.action_agents
    timeout = request.app.state.settings.action_timeout_s
    task = ActionTask(approval_id=a.id, incident_id=a.incident_id, action=a.action,
                      args=a.action_args)  # fmt: skip
    try:
        res = await agents.run(EXECUTOR, task, user_token, timeout_s=timeout)
    except CallError as exc:
        log.warning("action_result_unknown", approval_id=str(a.id), error=str(exc))
        return DecisionResult(approval=a, execution_error=(
            f"Result unknown ({exc}). {_retry_hint(a.id)}")[:1000])  # fmt: skip
    ex = res.action
    if ex is None:
        return DecisionResult(approval=a, execution_error=(
            f"The executor returned no result ({res.error}). {_retry_hint(a.id)}")[:1000])  # fmt: skip
    if ex.status == "EXECUTED":
        outcome = OutcomeIn(outcome="EXECUTED", tool_call_id=ex.tool_call_id,
                            detail={"result": ex.result})  # fmt: skip
    elif ex.reason == ALREADY_EXECUTED:  # ex.tool_call_id = the refused call, not the run
        outcome = OutcomeIn(outcome="EXECUTED", tool_call_id=None,
                            detail={"note": "executed earlier; recorded on retry",
                                    "gateway": ex.detail})  # fmt: skip
    elif (ex.reason == TOOL_FAILED and ex.http_status is not None
          and 400 <= ex.http_status < 500 and ex.http_status != 429):  # fmt: skip
        outcome = OutcomeIn(outcome="FAILED", tool_call_id=ex.tool_call_id,
                            detail={"error": f"{ex.reason}: {ex.detail}"[:500],
                                    "http_status": ex.http_status})  # fmt: skip
    elif ex.http_status is not None and 400 <= ex.http_status < 500 and ex.http_status != 429:
        # a refused attempt: nothing ran in THIS call; whether an earlier one did is for the
        # gateway to say on a retry - never recorded as the action's outcome
        return DecisionResult(approval=a, execution_error=(
            f"Not executed: {ex.reason}: {ex.detail}")[:1000])  # fmt: skip
    else:  # 429 / 5xx / unknown: maybe it ran, maybe not - never record a guess
        return DecisionResult(approval=a, execution_error=(
            f"{ex.reason}: {ex.detail}. {_retry_hint(a.id)}")[:1000])  # fmt: skip
    try:
        a = await incidents.record_outcome(a.id, outcome)
    except CallError as exc:
        log.error("action_outcome_not_recorded", approval_id=str(a.id), error=str(exc))
        return DecisionResult(approval=a, executed=outcome.outcome == "EXECUTED",
                              result=ex.result, execution_error=(
            f"{outcome.outcome}, but the outcome was not recorded ({exc}). "
            f"{_retry_hint(a.id)}")[:1000])  # fmt: skip
    if outcome.outcome == "FAILED":
        return DecisionResult(approval=a, execution_error=f"{ex.reason}: {ex.detail}"[:1000])
    return DecisionResult(approval=a, executed=True, result=ex.result)


@router.post("/approvals/{approval_id}/decision", response_model=DecisionResult)
async def decide(
    approval_id: UUID, body: DecisionIn, request: Request, who: Approver
) -> DecisionResult:
    """Never retried by us: a decision is sent once. A client retry of an APPROVED decision
    gets 409 (already decided) - use /execute for the action."""
    incidents: IncidentClient = request.app.state.incidents
    token = _bearer(request)
    try:
        a = await incidents.decide(approval_id, body, token)
    except CallError as exc:
        _raise(exc)
        raise  # pragma: no cover - _raise always raises
    log.info("approval_decided", approval_id=str(approval_id), decision=body.decision,
             actor=who.actor)  # fmt: skip
    if a.status != "APPROVED":
        return DecisionResult(approval=a)
    return await execute(request, a, token)


@router.post("/approvals/{approval_id}/execute", response_model=DecisionResult)
async def execute_again(approval_id: UUID, request: Request, who: Approver) -> DecisionResult:
    incidents: IncidentClient = request.app.state.incidents
    token = _bearer(request)
    try:
        a = await incidents.get_approval(approval_id, token)
    except CallError as exc:
        _raise(exc)
        raise  # pragma: no cover
    if a.status != "APPROVED":
        raise ConflictError(f"Approval is {a.status}: only an APPROVED action with no "
                            "recorded outcome can be executed.")  # fmt: skip
    if a.decided_by != who.user_id:
        # the gateway refuses it too; say it here, before calling three services
        raise ForbiddenError("Only the approver can execute this approval.")
    return await execute(request, a, token)
