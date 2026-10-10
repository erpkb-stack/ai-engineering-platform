"""HTTP routes (internal API, called by the api gateway). Prefix /v1.

Every route re-checks permissions from the verified token, even though the gateway already
did (defence in depth: a misrouted or direct call must not bypass authorisation).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import NotFoundError
from aeoi_incident import postmortem, service
from aeoi_incident.config import Settings
from aeoi_incident.db import session_dep
from aeoi_incident.domain import parse_if_match, parse_incident_ref
from aeoi_incident.idempotency import Result, run_idempotent, validate_key
from aeoi_models.api.agents import EvidenceBatch, EvidenceBatchResult
from aeoi_models.api.common import Page
from aeoi_models.api.hypotheses import HypothesisBatch, HypothesisRecord
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
from aeoi_models.api.postmortem import PostmortemDoc, PostmortemIn, PostmortemStored, TimelineBatch
from aeoi_security.auth import Principal
from aeoi_security.rbac import EVIDENCE_KIND_PERMS, Perm, may_read_evidence
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


def _readable(who: Principal) -> list[str]:
    return [k for k in EVIDENCE_KIND_PERMS if may_read_evidence(who.has, k)]


@router.get("/incidents/{ref}/timeline", response_model=list[TimelineEntry])
async def get_timeline(
    ref: str, session: Session, who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))]
) -> Response:
    """Human actions + agent-observed points (Phase 12), in time order. A row whose source is
    an evidence kind (LOG, METRIC, DEPLOY, ...) needs that kind's permission (ADR-020)."""
    rows, hidden = await postmortem.timeline_for(session, parse_incident_ref(ref), _readable(who))
    body = [TimelineEntry.model_validate(r).model_dump(mode="json") for r in rows]
    # rows are filtered in SQL; the header says how many, so a client knows the list is partial
    return JSONResponse(body, headers={"X-Hidden-Rows": str(hidden)})


@router.post("/incidents/{ref}/timeline", response_model=EvidenceBatchResult)
async def add_timeline(
    ref: str,
    batch: TimelineBatch,
    session: Session,
    who: Annotated[Principal, Depends(require_scope("timeline:write"))],
) -> EvidenceBatchResult:
    """Service-only (orchestrator, ADR-022): code-written observation points. Idempotent."""
    async with session.begin():
        received, inserted = await postmortem.add_timeline(
            session, parse_incident_ref(ref), batch, who
        )
    return EvidenceBatchResult(received=received, inserted=inserted)


@router.post("/incidents/{ref}/postmortems", response_model=PostmortemStored)
async def add_postmortem(
    ref: str,
    body: PostmortemIn,
    session: Session,
    who: Annotated[Principal, Depends(require_scope("postmortems:write"))],
) -> PostmortemStored:
    """Service-only (orchestrator, ADR-022). Idempotent per (incident, investigation)."""
    async with session.begin():
        inserted = await postmortem.add_postmortem(session, parse_incident_ref(ref), body, who)
    return PostmortemStored(received=True, inserted=inserted)


@router.get("/incidents/{ref}/postmortem", response_model=PostmortemDoc)
async def get_postmortem(
    ref: str,
    session: Session,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
    investigation_id: UUID | None = None,
    format: Annotated[str, Query(pattern=r"^(json|markdown)$")] = "json",
) -> Response:
    """Assembled at read time (ADR-022): numbers and root cause by code, model sentences
    redacted unless the reader may read all the evidence they cite."""
    doc = await postmortem.postmortem_doc(
        session, parse_incident_ref(ref), _readable(who), investigation_id
    )
    if doc is None:
        raise NotFoundError("No postmortem for this incident yet.")
    if format == "markdown":
        return PlainTextResponse(postmortem.render_markdown(doc), media_type="text/markdown")
    return JSONResponse(doc.model_dump(mode="json"))


@router.get("/incidents/{ref}/evidence", response_model=list[EvidenceOut])
async def get_evidence(
    ref: str,
    session: Session,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
    kind: Annotated[str | None, Query(pattern=r"^[A-Z]{2,10}$")] = None,
) -> list[EvidenceOut]:
    # each kind needs the permission of the tool that produced it (a MANAGER may read the
    # incident but not its raw log lines). Hidden kinds are simply absent.
    readable = [k for k in EVIDENCE_KIND_PERMS if may_read_evidence(who.has, k)]
    rows = await service.evidence(session, parse_incident_ref(ref), kind, readable)
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


@router.post("/incidents/{ref}/hypotheses", response_model=EvidenceBatchResult)
async def add_hypotheses(
    ref: str,
    batch: HypothesisBatch,
    session: Session,
    who: Annotated[Principal, Depends(require_scope("hypotheses:write"))],
) -> EvidenceBatchResult:
    """Service-only (orchestrator, ADR-021): validated hypotheses + evidence edges.
    Idempotent per (investigation, key)."""
    async with session.begin():
        received, inserted = await service.add_hypotheses(
            session, parse_incident_ref(ref), batch, who
        )
    return EvidenceBatchResult(received=received, inserted=inserted)


@router.get("/incidents/{ref}/hypotheses", response_model=list[HypothesisRecord])
async def get_hypotheses(
    ref: str,
    session: Session,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
    investigation_id: UUID | None = None,
) -> list[HypothesisRecord]:
    readable = [k for k in EVIDENCE_KIND_PERMS if may_read_evidence(who.has, k)]
    return await service.hypotheses(session, parse_incident_ref(ref), investigation_id, readable)


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
