"""Public API /api/v1 (Feature 19). Typed routes -> a real OpenAPI contract for the frontend.

Order of checks on every route: authenticate (401) -> rate limit (429) -> permission (403)
-> validate body (422) -> forward. Unauthorised requests never reach internal services.

Spec endpoints that belong to later phases (approvals, agents, trace, metrics) are added by
those phases - not stubbed here, so the OpenAPI never lies. Search/documents: Phase 6.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from aeoi_api.proxy import Upstream
from aeoi_common.errors import RateLimitedError
from aeoi_models.api.audit import AuditPage
from aeoi_models.api.common import Page
from aeoi_models.api.incidents import (
    EvidenceOut,
    FeedbackCreate,
    FeedbackOut,
    IncidentCreate,
    IncidentOut,
    IncidentPatch,
    InvestigationAccepted,
    TimelineEntry,
)
from aeoi_models.api.search import DocumentOut, SearchRequest, SearchResponse
from aeoi_models.api.tools import ToolInfo, ToolInvokeRequest, ToolResult
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web.auth import current_principal

router = APIRouter(prefix="/api/v1")


def guard(perm: Perm):  # type: ignore[no-untyped-def]  # returns a FastAPI dependency
    async def _dep(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        wait = await request.app.state.limiter.acquire(principal.subject)
        if wait > 0:
            raise RateLimitedError(f"Rate limit exceeded. Retry in {wait:.1f}s.")
        if not principal.has(perm):
            from aeoi_common.errors import ForbiddenError

            raise ForbiddenError(f"Missing permission '{perm}'.")
        return principal

    return _dep


def authenticated():  # type: ignore[no-untyped-def]  # returns a FastAPI dependency
    """Rate limit + authentication only. For routes whose authorization is inherently
    per-item and lives in the service (which tool; which audit scope). Not a shortcut: the
    service is the policy point, and it audits the decision."""

    async def _dep(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        wait = await request.app.state.limiter.acquire(principal.subject)
        if wait > 0:
            raise RateLimitedError(f"Rate limit exceeded. Retry in {wait:.1f}s.")
        return principal

    return _dep


def incidents(request: Request) -> Upstream:
    return request.app.state.upstreams["incident-service"]  # type: ignore[no-any-return]


Incidents = Annotated[Upstream, Depends(incidents)]


def rag(request: Request) -> Upstream:
    return request.app.state.upstreams["rag"]  # type: ignore[no-any-return]


Rag = Annotated[Upstream, Depends(rag)]


def tools(request: Request) -> Upstream:
    return request.app.state.upstreams["tool-gateway"]  # type: ignore[no-any-return]


Tools = Annotated[Upstream, Depends(tools)]


def audit(request: Request) -> Upstream:
    return request.app.state.upstreams["audit"]  # type: ignore[no-any-return]


Audit = Annotated[Upstream, Depends(audit)]


def _json(model: BaseModel) -> bytes:
    return model.model_dump_json(exclude_unset=True).encode()


class Me(BaseModel):
    user_id: str
    email: str
    name: str
    roles: list[str]
    groups: list[str]
    permissions: list[str]


@router.get("/me", response_model=Me, tags=["identity"])
async def me(principal: Annotated[Principal, Depends(current_principal)]) -> Me:
    return Me(
        user_id=str(principal.user_id),
        email=principal.email,
        name=principal.name,
        roles=sorted(principal.roles),
        groups=sorted(principal.groups),
        permissions=sorted(p.value for p in principal.permissions),
    )


@router.post("/incidents", status_code=201, response_model=IncidentOut, tags=["incidents"])
async def create_incident(
    body: IncidentCreate,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.INCIDENTS_CREATE))],
) -> Response:
    return await up.forward(request, "/v1/incidents", body=_json(body))


@router.get("/incidents", response_model=Page[IncidentOut], tags=["incidents"])
async def list_incidents(
    request: Request, up: Incidents, _: Annotated[Principal, Depends(guard(Perm.INCIDENTS_READ))]
) -> Response:
    return await up.forward(request, "/v1/incidents")


@router.get("/incidents/{ref}", response_model=IncidentOut, tags=["incidents"])
async def get_incident(
    ref: str,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.INCIDENTS_READ))],
) -> Response:
    return await up.forward(request, f"/v1/incidents/{ref}")


@router.patch("/incidents/{ref}", response_model=IncidentOut, tags=["incidents"])
async def patch_incident(
    ref: str,
    body: IncidentPatch,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.INCIDENTS_WRITE))],
) -> Response:
    return await up.forward(request, f"/v1/incidents/{ref}", body=_json(body))


@router.post(
    "/incidents/{ref}/investigate",
    status_code=202,
    response_model=InvestigationAccepted,
    tags=["incidents"],
)
async def investigate(
    ref: str,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.INVESTIGATIONS_RUN))],
) -> Response:
    return await up.forward(request, f"/v1/incidents/{ref}/investigate", body=b"")


@router.get("/incidents/{ref}/timeline", response_model=list[TimelineEntry], tags=["incidents"])
async def timeline(
    ref: str,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.INCIDENTS_READ))],
) -> Response:
    return await up.forward(request, f"/v1/incidents/{ref}/timeline")


@router.get("/incidents/{ref}/evidence", response_model=list[EvidenceOut], tags=["incidents"])
async def evidence(
    ref: str,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.INCIDENTS_READ))],
) -> Response:
    return await up.forward(request, f"/v1/incidents/{ref}/evidence")


@router.post("/feedback", status_code=201, response_model=FeedbackOut, tags=["feedback"])
async def feedback(
    body: FeedbackCreate,
    request: Request,
    up: Incidents,
    _: Annotated[Principal, Depends(guard(Perm.FEEDBACK_WRITE))],
) -> Response:
    return await up.forward(request, "/v1/feedback", body=_json(body))


@router.post("/search", response_model=SearchResponse, tags=["search"])
async def search(
    body: SearchRequest,
    request: Request,
    up: Rag,
    _: Annotated[Principal, Depends(guard(Perm.DOCS_READ))],
) -> Response:
    """Permission-aware hybrid search. The caller's groups come from the token, in rag."""
    return await up.forward(request, "/v1/search", body=_json(body))


@router.get("/documents/{document_id}", response_model=DocumentOut, tags=["search"])
async def get_document(
    document_id: UUID,
    request: Request,
    up: Rag,
    _: Annotated[Principal, Depends(guard(Perm.DOCS_READ))],
) -> Response:
    return await up.forward(request, f"/v1/documents/{document_id}")


@router.get("/tools", response_model=list[ToolInfo], tags=["tools"])
async def list_tools(
    request: Request, up: Tools, _: Annotated[Principal, Depends(authenticated())]
) -> Response:
    """Tools the caller's permissions allow (manual mode: the user is the caller)."""
    return await up.forward(request, "/v1/tools")


@router.post("/tools/{name}/invoke", response_model=ToolResult, tags=["tools"])
async def invoke_tool(
    name: str,
    body: ToolInvokeRequest,
    request: Request,
    up: Tools,
    _: Annotated[Principal, Depends(authenticated())],
) -> Response:
    """Manual tool call. The tool-gateway authorizes (per tool), sanitises and AUDITS it.
    X-On-Behalf-Of is never forwarded from the edge: only internal services may use it."""
    return await up.forward(request, f"/v1/tools/{name}/invoke", body=_json(body))


@router.get("/audit/events", response_model=AuditPage, tags=["audit"])
async def audit_events(
    request: Request, up: Audit, _: Annotated[Principal, Depends(authenticated())]
) -> Response:
    """Scope (all / incident / own) is decided by the audit service from permissions."""
    return await up.forward(request, "/v1/events")
