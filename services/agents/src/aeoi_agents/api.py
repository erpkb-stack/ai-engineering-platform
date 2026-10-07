"""HTTP API. Phase 8: synchronous task endpoint called by the orchestrator runner.
[Phase 18] the same handler is driven by an AgentTask Kafka consumer instead.

Caller = SERVICE token with scope `agents:run` + `X-On-Behalf-Of: Bearer <user JWT>`.
The worker verifies the user token itself (it forwards it to the tool gateway) and never
accepts a user id from the body.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from aeoi_common.correlation import get_correlation_id
from aeoi_common.errors import ForbiddenError
from aeoi_models.api.agents import AgentRunResult, LogAnalysisTask
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER
from aeoi_security.auth import AuthError, Principal
from aeoi_web import require_scope

router = APIRouter(prefix="/v1/agents", tags=["agents"])
Caller = Annotated[Principal, Depends(require_scope("agents:run"))]


def _user_token(request: Request) -> str:
    raw = request.headers.get(ON_BEHALF_OF_HEADER, "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ForbiddenError(f"{ON_BEHALF_OF_HEADER}: Bearer <user token> is required.")
    try:
        user = request.app.state.auth.verify(token.strip())
    except AuthError as exc:
        raise ForbiddenError("On-behalf-of token is invalid.") from exc
    if user.is_service:
        raise ForbiddenError("On-behalf-of must be a USER token.")
    return token.strip()


@router.post("/log_analysis/run", response_model=AgentRunResult)
async def run_log_analysis(task: LogAnalysisTask, request: Request, _: Caller) -> AgentRunResult:
    user_token = _user_token(request)
    agent = request.app.state.log_agent
    result: AgentRunResult = await agent.run(task, user_token, get_correlation_id())
    return result
