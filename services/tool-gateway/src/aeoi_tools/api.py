"""HTTP API.

Two caller modes (ADR-017):
- USER (manual mode): user JWT. agent = None. Effective perms = the user's perms.
- AGENT: service JWT with scope `tools:invoke` + `X-On-Behalf-Of: <the user's JWT>` +
  body `agent_name`. The service must be trusted (agents.yaml) to assert that agent name.
  Effective perms = user perms ∩ agent allow-list.
A service token alone can do nothing: tools always run FOR a verified human.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from aeoi_common.correlation import get_correlation_id
from aeoi_common.errors import (
    ForbiddenError,
    NotFoundError,
    UpstreamUnavailableError,
)
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER, ToolInfo, ToolInvokeRequest, ToolResult
from aeoi_security.auth import AuthError, Principal
from aeoi_tools.contracts import ToolContext
from aeoi_tools.policy import DenyReason, decide
from aeoi_tools.registry import describe
from aeoi_tools.service import AuditWriteError, Outcome, ToolService
from aeoi_web import CurrentPrincipal
from aeoi_web.auth import Authenticator
from aeoi_web.problems import PROBLEM_JSON

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["tools"])
SCOPE = "tools:invoke"
_TITLES = {
    400: "Bad request",
    403: "Forbidden",
    413: "Request too large",
    404: "Not found",
    422: "Validation failed",
    429: "Too many requests",
    501: "Not implemented",
    502: "Bad gateway",
    503: "Dependency unavailable",
    504: "Tool timeout",
    500: "Internal error",
}


class _ServiceDeniedError(Exception):
    def __init__(self, status: int, reason: DenyReason | str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.reason, self.detail = status, reason, detail


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _context(
    request: Request, principal: Principal, agent_name: str | None, ids: dict[str, Any]
) -> ToolContext:
    """Turn verified tokens into a ToolContext. Raises _ServiceDeniedError for refused services."""
    obo_header = request.headers.get(ON_BEHALF_OF_HEADER)
    if not principal.is_service:
        if obo_header:
            raise _ServiceDeniedError(
                400,
                DenyReason.INVALID_ON_BEHALF_OF,
                f"{ON_BEHALF_OF_HEADER} is only accepted from service tokens.",
            )
        if agent_name:
            raise _ServiceDeniedError(
                403, DenyReason.UNTRUSTED_AGENT_ASSERTION, "Users cannot act as an agent."
            )
        token = _bearer(request.headers.get("authorization")) or ""
        return ToolContext(
            user=principal,
            user_token=token,
            agent=None,
            service=None,
            correlation_id=get_correlation_id(),
            **ids,
        )
    if SCOPE not in principal.scopes:
        raise _ServiceDeniedError(403, DenyReason.MISSING_PERMISSION, f"Requires scope '{SCOPE}'.")
    obo = _bearer(obo_header)
    if obo is None:
        raise _ServiceDeniedError(
            403,
            DenyReason.SERVICE_WITHOUT_USER,
            f"Service calls need {ON_BEHALF_OF_HEADER}: Bearer <user token>.",
        )
    auth: Authenticator = request.app.state.auth
    try:
        user = auth.verify(obo)
    except AuthError as exc:
        # 403, not 401: the CALLER's own token is fine; a 401 would make it refresh and retry
        raise _ServiceDeniedError(
            403, DenyReason.INVALID_ON_BEHALF_OF, "On-behalf-of token is invalid."
        ) from exc
    if user.is_service:
        raise _ServiceDeniedError(
            403, DenyReason.INVALID_ON_BEHALF_OF, "On-behalf-of must be a USER token."
        )
    service = principal.name
    if not agent_name or not request.app.state.service.policy.may_assert(service, agent_name):
        raise _ServiceDeniedError(
            403,
            DenyReason.UNTRUSTED_AGENT_ASSERTION,
            f"Service '{service}' may not act as agent '{agent_name}'.",
        )
    return ToolContext(
        user=user,
        user_token=obo,
        agent=agent_name,
        service=principal.subject,
        correlation_id=get_correlation_id(),
        **ids,
    )


def _problem(status: int, detail: str, path: str, **extra: Any) -> JSONResponse:
    body = {
        "type": f"https://aeoi.example/problems/tool-{extra.get('reason') or 'error'}",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "instance": path,
        "correlation_id": get_correlation_id(),
        **{k: v for k, v in extra.items() if v is not None},
    }
    headers = {"Retry-After": str(max(1, round(extra.get("retry_after_s") or 1)))}
    return JSONResponse(
        body,
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=headers if status in (429, 503) else None,
    )


def _respond(out: Outcome, path: str) -> JSONResponse:
    if out.status == "OK" and out.data is not None:
        result = ToolResult(
            tool_call_id=out.call_id,
            tool=out.tool,
            data=out.data,
            evidence_ids=out.evidence_ids,
            truncated=bool(out.data.get("truncated")),
            security=out.security,  # type: ignore[arg-type]
            latency_ms=out.latency_ms,
            attempts=out.attempts,
        )
        return JSONResponse(result.model_dump(mode="json"))
    return _problem(
        out.http_status,
        out.detail,
        path,
        reason=str(out.reason) if out.reason else None,
        tool_call_id=str(out.call_id),
        errors=out.errors,
        retry_after_s=out.retry_after_s,
    )


async def _refuse(
    svc: ToolService,
    principal: Principal,
    name: str,
    path: str,
    *,
    status: int,
    reason: str,
    detail: str,
    raw_args: Any,
    ids: dict[str, Any],
    call_status: str = "DENIED",
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """Record a refusal that happened before invoke(). A known USER -> a normal tool_calls row.
    A SERVICE (no verified user yet) -> an audit event with the service as actor."""
    try:
        if not principal.is_service:
            ctx = ToolContext(
                user=principal,
                user_token="",
                agent=None,
                service=None,
                correlation_id=get_correlation_id(),
                **ids,
            )
            out = await svc.record_refusal(
                name,
                raw_args,
                ctx,
                reason=reason,
                detail=detail,
                http_status=status,
                status=call_status,
                errors=errors,
            )
            return _respond(out, path)
        event_id = await svc.record_service_denial(
            service=principal.name,
            tool=name,
            reason=reason,
            detail=detail,
            correlation_id=get_correlation_id(),
        )
    except AuditWriteError as err:
        raise UpstreamUnavailableError("Cannot record the call; refusing it.") from err
    return _problem(
        status,
        detail,
        path,
        reason=reason,
        errors=errors,
        audit_event_id=str(event_id) if event_id else None,
    )


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(
    request: Request, principal: CurrentPrincipal, agent_name: str | None = None
) -> Any:
    """The tools THIS caller may use (perms ∩ allow-list), with JSON schemas. Not audited:
    listing reads nothing but the registry."""
    svc: ToolService = request.app.state.service
    try:
        ctx = await _context(request, principal, agent_name, {})
    except _ServiceDeniedError as exc:
        raise ForbiddenError(exc.detail) from exc
    return [
        describe(spec)
        for spec in svc.registry.values()
        if decide(spec, ctx, svc.policy).allowed
        or decide(spec, ctx, svc.policy).reason
        in (DenyReason.APPROVAL_REQUIRED, DenyReason.APPROVAL_UNVERIFIABLE)
    ]


@router.get("/tools/{name}", response_model=ToolInfo)
async def get_tool(name: str, request: Request, principal: CurrentPrincipal) -> Any:
    svc: ToolService = request.app.state.service
    spec = svc.registry.get(name)
    if spec is None:
        raise NotFoundError("No such tool.")
    return describe(spec)


@router.post(
    "/tools/{name}/invoke",
    response_model=ToolResult,
    responses={403: {}, 404: {}, 422: {}, 429: {}, 503: {}, 504: {}},
)
async def invoke(name: str, request: Request, principal: CurrentPrincipal) -> JSONResponse:
    svc: ToolService = request.app.state.service
    path = request.url.path
    raw = await request.body()
    if len(raw) > request.app.state.settings.max_request_bytes:
        # never parsed, never stored: we keep only its size
        return await _refuse(
            svc, principal, name, path, status=413, reason="request_too_large",
            detail="Request body too large.", raw_args={"_truncated": True, "size_bytes": len(raw)},
            ids={}, call_status="ERROR",
        )  # fmt: skip
    try:
        body = ToolInvokeRequest.model_validate_json(raw or b"{}")
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]]
        return await _refuse(
            svc, principal, name, path, status=422, reason="invalid_request",
            detail="Invalid request envelope.", raw_args={"_invalid_envelope": True},
            ids={}, call_status="ERROR", errors=errors,
        )  # fmt: skip
    ids = {
        "incident_id": body.incident_id,
        "investigation_id": body.investigation_id,
        "task_id": body.task_id,
        "approval_id": body.approval_id,
    }
    try:
        ctx = await _context(request, principal, body.agent_name, ids)
    except _ServiceDeniedError as exc:
        return await _refuse(
            svc, principal, name, path, status=exc.status, reason=str(exc.reason),
            detail=exc.detail, raw_args=body.args, ids=ids,
        )  # fmt: skip
    try:
        out = await svc.invoke(name, body.args, ctx)
    except AuditWriteError as exc:
        raise UpstreamUnavailableError("Cannot record the call; refusing it.") from exc
    return _respond(out, path)
