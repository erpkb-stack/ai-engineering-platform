"""Release risk API (Phase 15, FR-8, ADR-025). ADVISORY: nothing here or elsewhere reads an
assessment to gate a deploy.

POST runs the release_risk agent SYNCHRONOUSLY with the caller's own token (forwarded as
on-behalf-of, never stored - the same rule as an investigation start), then records the
result. Each POST is a new snapshot (the data can change between runs); an optional
Idempotency-Key makes a RETRY of the same request return the first report (api-design rule).
Every read needs code:read - a report quotes code (the hunk with the finding).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aeoi_common.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamUnavailableError,
)
from aeoi_common.ids import uuid7
from aeoi_db.models.orchestrator import ReleaseAssessment
from aeoi_models.api.release import (
    EvidenceOut,
    ReleaseRiskOut,
    ReleaseRiskReport,
    ReleaseRiskRequest,
    ReleaseRiskTask,
)
from aeoi_orchestrator.clients import AgentsClient, CallError
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web import require

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["release-risk"])
CodeReader = Annotated[Principal, Depends(require(Perm.CODE_READ))]


def _readable(who: Principal) -> None:
    """A report quotes code AND deploy records: both permissions, on every read and write."""
    if not who.has(Perm.DEPLOYS_READ):
        raise ForbiddenError("release risk reads deploys and code: deploys:read is required.")


IdemKey = Annotated[
    str | None, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9._:-]{8,128}$")
]
AGENT = "release_risk"


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _out(row: ReleaseAssessment) -> ReleaseRiskOut:
    return ReleaseRiskOut(
        id=row.id, status=row.status, band=row.band,  # type: ignore[arg-type]
        requested_by=UUID(row.requested_by), requested_at=row.requested_at,
        report=ReleaseRiskReport.model_validate(row.report) if row.report else None,
        evidence=[EvidenceOut.model_validate(e) for e in row.evidence],
        error=row.error, agent_version=row.agent_version,
    )  # fmt: skip


@router.post("/release-risk", status_code=201, response_model=ReleaseRiskOut)
async def assess(
    body: ReleaseRiskRequest,
    request: Request,
    response: Response,
    who: CodeReader,
    idempotency_key: IdemKey = None,
) -> ReleaseRiskOut:
    """201 with the stored report - also when the target could not be resolved (status
    FAILED, the reason in `error`): a request that reached the agent is recorded. 503 (nothing
    recorded) when the agent could not be reached at all. A retry with the same
    Idempotency-Key (same user) returns the first report with 200 and runs nothing; the same
    key with a DIFFERENT request is 409."""
    _readable(who)
    if idempotency_key:
        first = await _by_key(request, str(who.user_id), idempotency_key)
        if first is not None:
            return _replay(first, body, response)
    agents: AgentsClient = request.app.state.release_agents
    aid = uuid7()
    try:
        res = await agents.run(AGENT, ReleaseRiskTask(assessment_id=aid, request=body),
                               _bearer(request))  # fmt: skip
    except CallError as exc:
        if exc.status == 403:
            raise ForbiddenError(str(exc)) from exc
        raise UpstreamUnavailableError(f"release risk not assessed: {exc}") from exc
    rep = res.release or ReleaseRiskReport(target_kind=body.kind, repository=body.repository,
                                           pull_request=body.pull_request,
                                           deploy_key=body.deploy_key,
                                           service_key=body.service_key, base_sha=body.base_sha,
                                           head_sha=body.head_sha)  # fmt: skip
    status = "FAILED" if res.status == "FAILED" else ("PARTIAL" if res.degraded else "SUCCEEDED")
    evidence: list[dict[str, Any]] = [
        EvidenceOut(evidence_key=e.evidence_key, kind=e.kind, title=e.title, excerpt=e.excerpt,
                    uri=e.uri).model_dump()
        for e in res.evidence
    ]  # fmt: skip
    row = ReleaseAssessment(
        id=aid, requested_by=str(who.user_id), idempotency_key=idempotency_key,
        target_kind=body.kind,
        repository=body.repository, pull_request=body.pull_request, deploy_key=body.deploy_key,
        service_key=rep.service_key or body.service_key, base_sha=rep.base_sha or body.base_sha,
        head_sha=rep.head_sha or body.head_sha,
        status=status, band=None if status == "FAILED" else rep.band,
        report=rep.model_dump(mode="json"), evidence=evidence,
        error=(res.error or res.degraded or None), agent_version=res.trace.agent_version,
    )  # fmt: skip
    try:
        async with request.app.state.store.sessions() as s, s.begin():
            s.add(row)
    except IntegrityError as exc:
        # ONLY the per-requester key index: two concurrent requests with one key - the other one
        # stored first, return its report (both ran the agent: recorded, accepted [P]). Any other
        # integrity error (a CHECK) is a bug and must surface (review).
        if "uq_release_assessments_requester_idempotency_key" not in str(exc.orig):
            raise
        first = await _by_key(request, str(who.user_id), idempotency_key or "")
        if first is None:
            raise
        return _replay(first, body, response)
    async with request.app.state.store.sessions() as s:
        stored = await s.get(ReleaseAssessment, aid)
    assert stored is not None  # noqa: S101 - just written in this request
    log.info("release_risk.assessed", assessment_id=str(aid), status=status, band=row.band,
             target=body.label())  # fmt: skip
    return _out(stored)


async def _by_key(request: Request, user: str, key: str) -> ReleaseAssessment | None:
    q = select(ReleaseAssessment).where(
        ReleaseAssessment.requested_by == user, ReleaseAssessment.idempotency_key == key
    )
    async with request.app.state.store.sessions() as s:
        found: ReleaseAssessment | None = (await s.scalars(q)).first()
    return found


def _replay(
    first: ReleaseAssessment, body: ReleaseRiskRequest, response: Response
) -> ReleaseRiskOut:
    target = (first.target_kind, first.repository, first.pull_request, first.deploy_key)
    same = target == (body.kind, body.repository, body.pull_request, body.deploy_key) and (
        body.kind != "range"
        or (first.service_key, first.base_sha, first.head_sha)
        == (body.service_key, body.base_sha, body.head_sha)
    )
    if not same:
        raise ConflictError("this Idempotency-Key was used for a different release target")
    response.status_code = 200
    return _out(first)


@router.get("/release-risk/{assessment_id}", response_model=ReleaseRiskOut)
async def get_assessment(assessment_id: UUID, request: Request, who: CodeReader) -> ReleaseRiskOut:
    _readable(who)
    async with request.app.state.store.sessions() as s:
        row = await s.get(ReleaseAssessment, assessment_id)
    if row is None:
        raise NotFoundError(f"release assessment {assessment_id} not found")
    return _out(row)


@router.get("/release-risk", response_model=list[ReleaseRiskOut])
async def list_assessments(
    request: Request,
    who: CodeReader,
    service_key: Annotated[str | None, Query(pattern=r"^[a-z0-9-]{2,100}$")] = None,
    deploy_key: Annotated[str | None, Query(pattern=r"^DEPLOY-[0-9]{1,24}$")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[ReleaseRiskOut]:
    _readable(who)
    if not (service_key or deploy_key):
        raise BadRequestError("give service_key or deploy_key (no full listing)")
    q = select(ReleaseAssessment).order_by(ReleaseAssessment.requested_at.desc()).limit(limit)
    if service_key:
        q = q.where(ReleaseAssessment.service_key == service_key)
    if deploy_key:
        q = q.where(ReleaseAssessment.deploy_key == deploy_key)
    async with request.app.state.store.sessions() as s:
        rows = list((await s.scalars(q)).all())
    return [_out(r) for r in rows]
