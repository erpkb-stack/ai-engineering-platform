"""HTTP routes (internal API, called by the api gateway). Prefix /v1.

Every route re-checks permissions from the verified token, even though the gateway already
did (defence in depth: a misrouted or direct call must not bypass authorisation).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_incident import service
from aeoi_incident.config import Settings
from aeoi_incident.db import session_dep
from aeoi_incident.domain import parse_if_match, parse_incident_ref
from aeoi_incident.idempotency import Result, run_idempotent, validate_key
from aeoi_models.api.agents import EvidenceBatch, EvidenceBatchResult
from aeoi_models.api.common import Page
from aeoi_models.api.incidents import (
    EvidenceOut,
    FeedbackCreate,
    FeedbackOut,
    IncidentCreate,
    IncidentOut,
    IncidentPatch,
    IncidentStatus,
    InvestigationAccepted,
    Severity,
    TimelineEntry,
)
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web.auth import require, require_scope

router = APIRouter(prefix="/v1", tags=["incidents"])
Session = Annotated[AsyncSession, Depends(session_dep)]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.sessionmaker  # type: ignore[no-any-return]


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _etag(version: int) -> str:
    return f'"{version}"'


def _result_response(result: Result, *, location: str | None = None) -> JSONResponse:
    headers: dict[str, str] = {}
    if result.replayed:
        headers["Idempotent-Replayed"] = "true"
    if location:
        headers["Location"] = location
    if "version" in result.body:
        headers["ETag"] = _etag(int(result.body["version"]))
    return JSONResponse(result.body, status_code=result.status, headers=headers)


@router.post(
    "/incidents",
    status_code=201,
    response_model=IncidentOut,
    responses={201: {"description": "Created (or replayed: header Idempotent-Replayed)"}},
)
async def create_incident(
    body: IncidentCreate,
    request: Request,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_CREATE))],
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    key = validate_key(idempotency_key, required=True)

    async def work(session: AsyncSession) -> tuple[int, dict[str, object]]:
        incident = await service.create_incident(session, body, who)
        return 201, service.to_out(incident).model_dump(mode="json")

    result = await run_idempotent(
        _sessions(request),
        principal=who.subject,
        scope="POST /v1/incidents",
        key=key,
        body=body.model_dump(mode="json"),
        ttl=_settings(request).idempotency_ttl,
        work=work,
    )
    return _result_response(result, location=f"/v1/incidents/{result.body['id']}")


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    session: Session,
    request: Request,
    _: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
    status: IncidentStatus | None = None,
    severity: Severity | None = None,
    service_key: Annotated[str | None, Query(alias="service", max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> Page[IncidentOut]:
    rows, next_cursor = await service.list_incidents(
        session,
        status=status,
        severity=severity.value if severity else None,
        service=service_key,
        limit=min(limit, _settings(request).page_size_max),
        cursor=cursor,
    )
    return Page[IncidentOut](items=[service.to_out(r) for r in rows], next_cursor=next_cursor)


@router.get("/incidents/{ref}", response_model=IncidentOut)
async def get_incident(
    ref: str,
    session: Session,
    response: Response,
    _: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
) -> IncidentOut:
    incident = await service.load(session, parse_incident_ref(ref))
    response.headers["ETag"] = _etag(incident.version)
    return service.to_out(incident)


@router.patch("/incidents/{ref}", response_model=IncidentOut)
async def patch_incident(
    ref: str,
    body: IncidentPatch,
    session: Session,
    response: Response,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> IncidentOut:
    from aeoi_common.errors import PreconditionRequiredError

    expected = parse_if_match(if_match)
    if expected is None:
        raise PreconditionRequiredError(
            'Send If-Match: "<version>" (from the ETag of your last read).'
        )
    async with session.begin():
        incident = await service.update_incident(
            session, parse_incident_ref(ref), body, expected, who
        )
        out = service.to_out(incident)
    response.headers["ETag"] = _etag(out.version)
    return out


@router.post("/incidents/{ref}/investigate", status_code=202, response_model=InvestigationAccepted)
async def investigate(
    ref: str,
    request: Request,
    who: Annotated[Principal, Depends(require(Perm.INVESTIGATIONS_RUN))],
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    key = validate_key(idempotency_key, required=True)
    incident_ref = parse_incident_ref(ref)

    async def work(session: AsyncSession) -> tuple[int, dict[str, object]]:
        incident, request_id = await service.request_investigation(session, incident_ref, who)
        return 202, InvestigationAccepted(
            request_id=request_id, incident_id=incident.id
        ).model_dump(mode="json")

    result = await run_idempotent(
        _sessions(request),
        principal=who.subject,
        scope=f"POST /v1/incidents/{ref}/investigate",
        key=key,
        body={"ref": ref},
        ttl=_settings(request).idempotency_ttl,
        work=work,
    )
    return _result_response(result)


@router.get("/incidents/{ref}/timeline", response_model=list[TimelineEntry])
async def get_timeline(
    ref: str, session: Session, _: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))]
) -> list[TimelineEntry]:
    rows = await service.timeline(session, parse_incident_ref(ref))
    return [TimelineEntry.model_validate(r) for r in rows]


@router.get("/incidents/{ref}/evidence", response_model=list[EvidenceOut])
async def get_evidence(
    ref: str,
    session: Session,
    _: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
    kind: Annotated[str | None, Query(pattern=r"^[A-Z]{2,10}$")] = None,
) -> list[EvidenceOut]:
    rows = await service.evidence(session, parse_incident_ref(ref), kind)
    return [EvidenceOut.model_validate(r) for r in rows]


@router.post("/incidents/{ref}/evidence", response_model=EvidenceBatchResult)
async def add_evidence(
    ref: str,
    batch: EvidenceBatch,
    session: Session,
    who: Annotated[Principal, Depends(require_scope("evidence:write"))],
) -> EvidenceBatchResult:
    """Service-only (orchestrator). Users never write evidence: it must come from a recorded
    tool call. Idempotent: resending the same batch is a no-op."""
    async with session.begin():
        received, inserted = await service.add_evidence(
            session, parse_incident_ref(ref), batch, who
        )
    return EvidenceBatchResult(received=received, inserted=inserted)


@router.post("/feedback", status_code=201, response_model=FeedbackOut)
async def post_feedback(
    body: FeedbackCreate,
    request: Request,
    who: Annotated[Principal, Depends(require(Perm.FEEDBACK_WRITE))],
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    key = validate_key(idempotency_key, required=False)

    async def work(session: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await service.create_feedback(session, body, who)
        return 201, FeedbackOut(id=row.id, created_at=row.created_at).model_dump(mode="json")

    result = await run_idempotent(
        _sessions(request),
        principal=who.subject,
        scope="POST /v1/feedback",
        key=key,
        body=body.model_dump(mode="json"),
        ttl=_settings(request).idempotency_ttl,
        work=work,
    )
    return _result_response(result)


__all__ = ["UUID", "router"]
