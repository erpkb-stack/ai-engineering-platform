"""HTTP API. One synchronous task endpoint per agent, called by the orchestrator.
[Phase 18] the same handler is driven by an AgentTask Kafka consumer instead.

Caller = SERVICE token with scope `agents:run` + `X-On-Behalf-Of: Bearer <user JWT>`.
The worker verifies the user token itself (it forwards it to the tool gateway) and never
accepts a user id from the body.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from aeoi_common.correlation import get_correlation_id
from aeoi_common.errors import ForbiddenError, NotFoundError
from aeoi_models.api.agents import AgentName, AgentRunResult, AgentTask
from aeoi_models.api.approvals import ActionTask
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_models.api.release import ReleaseRiskTask
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER
from aeoi_security.auth import AuthError, Principal
from aeoi_web import require_scope

router = APIRouter(prefix="/v1/agents", tags=["agents"])
Caller = Annotated[Principal, Depends(require_scope("agents:run"))]


def _user_token(request: Request, investigation_id: UUID | None, incident_id: UUID | None) -> str:
    raw = request.headers.get(ON_BEHALF_OF_HEADER, "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ForbiddenError(f"{ON_BEHALF_OF_HEADER}: Bearer <user token> is required.")
    try:
        # a user token, or a delegated token bound to one investigation (ADR-019)
        user = request.app.state.auth.verify_obo(token.strip())
    except AuthError as exc:
        raise ForbiddenError("On-behalf-of token is invalid.") from exc
    if user.delegation is not None and (
        user.delegation.investigation_id != investigation_id
        or user.delegation.incident_id != incident_id
    ):
        raise ForbiddenError(
            "Delegated token is bound to another investigation/incident than this task's."
        )
    return token.strip()


@router.post("/hypothesis/run", response_model=AgentRunResult)
async def run_hypothesis(task: ReasoningTask, request: Request, _: Caller) -> AgentRunResult:
    """Phase 11: no tool calls, but still bound to the investigation by the delegated token."""
    _user_token(request, task.investigation_id, task.incident_id)
    result: AgentRunResult = await request.app.state.hypothesis_agent.run(
        task, "", get_correlation_id()
    )
    return result


@router.post("/critic/run", response_model=AgentRunResult)
async def run_critic(task: ReasoningTask, request: Request, _: Caller) -> AgentRunResult:
    _user_token(request, task.investigation_id, task.incident_id)
    result: AgentRunResult = await request.app.state.critic_agent.run(
        task, "", get_correlation_id()
    )
    return result


@router.post("/postmortem/run", response_model=AgentRunResult)
async def run_postmortem(task: ReasoningTask, request: Request, _: Caller) -> AgentRunResult:
    """Phase 12 (ADR-022): narrative over the validated result; no tool calls."""
    _user_token(request, task.investigation_id, task.incident_id)
    result: AgentRunResult = await request.app.state.postmortem_agent.run(
        task, "", get_correlation_id()
    )
    return result


@router.post("/historical_incident/run", response_model=AgentRunResult)
async def run_history(task: ReasoningTask, request: Request, _: Caller) -> AgentRunResult:
    """Phase 13 (ADR-023): reasoning input (observations) + one tool call AS THE USER: the
    delegated token is forwarded, so rag applies the user's groups."""
    user_token = _user_token(request, task.investigation_id, task.incident_id)
    result: AgentRunResult = await request.app.state.history_agent.run(
        task, user_token, get_correlation_id()
    )
    return result


@router.post("/code_intelligence/run", response_model=AgentRunResult)
async def run_code(task: ReasoningTask, request: Request, _: Caller) -> AgentRunResult:
    """Phase 14 (ADR-024): reads the deployment agent's result, calls code tools AS THE USER
    (delegated token forwarded: rag applies the user's groups to code references)."""
    user_token = _user_token(request, task.investigation_id, task.incident_id)
    result: AgentRunResult = await request.app.state.code_agent.run(
        task, user_token, get_correlation_id()
    )
    return result


@router.post("/release_risk/run", response_model=AgentRunResult)
async def run_release_risk(task: ReleaseRiskTask, request: Request, _: Caller) -> AgentRunResult:
    """Phase 15 (ADR-025): advisory release risk, synchronous, AS THE USER. Not part of an
    investigation: a token delegated to an investigation is refused here (it is bound to that
    investigation and incident, and this task has neither)."""
    user_token = _user_token(request, None, None)
    result: AgentRunResult = await request.app.state.release_agent.run(
        task, user_token, get_correlation_id()
    )
    return result


@router.post("/action_executor/run", response_model=AgentRunResult)
async def run_action(task: ActionTask, request: Request, _: Caller) -> AgentRunResult:
    """Phase 16 (ADR-026): run ONE approved action AS THE APPROVER. The on-behalf-of token
    must be the approver's own user token: a delegated (investigation) token is refused here
    (bound to an investigation, and this task has none) and again by the gateway policy."""
    user_token = _user_token(request, None, None)
    result: AgentRunResult = await request.app.state.action_executor.run(
        task, user_token, get_correlation_id()
    )
    return result


@router.post("/{agent}/run", response_model=AgentRunResult)
async def run_agent(
    agent: AgentName, task: AgentTask, request: Request, _: Caller
) -> AgentRunResult:
    """`agent` is a closed Literal: an unknown name is a 422, never a dynamic lookup."""
    user_token = _user_token(request, task.investigation_id, task.incident_id)
    worker = request.app.state.agents.get(agent)
    if worker is None:  # pragma: no cover - every AgentName is registered in main.py
        raise NotFoundError(f"agent {agent} is not deployed here")
    result: AgentRunResult = await worker.run(task, user_token, get_correlation_id())
    return result
