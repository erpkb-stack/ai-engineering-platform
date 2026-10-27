"""HTTP routes (internal API, called by the api gateway). Prefix /v1.

Every route re-checks permissions from the verified token, even though the gateway already
did (defence in depth: a misrouted or direct call must not bypass authorisation).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from aeoi_incident import approvals, postmortem, service
from aeoi_incident.alerts import (
    ALERTS_PRINCIPAL,
    DEDUPE_TTL,
    AlertRejectedError,
    dedupe_key,
    service_of,
    triage,
)
from aeoi_incident.catalog import CatalogClient, CatalogEntry
from aeoi_incident.config import Settings
from aeoi_incident.db import session_dep
from aeoi_incident.domain import parse_if_match, parse_incident_ref
from aeoi_incident.idempotency import Result, run_idempotent, validate_key
from aeoi_models.api.agents import EvidenceBatch, EvidenceBatchResult
from aeoi_models.api.alerts import AlertBatch, AlertIn, AlertResult, AlertsAccepted
from aeoi_models.api.approvals import (
    ActionRequest,
    ApprovalOut,
    ConsumeIn,
    ConsumeOut,
    DecisionIn,
    OutcomeIn,
)
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
from aeoi_security.webhook import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    load_secret,
    read_capped,
    verify,
)
from aeoi_web.auth import require, require_acting_user, require_scope

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


# ------------------------------------------------------------------ Phase 16 (ADR-026)
def _code_reader(who: Principal) -> bool:
    return who.has(Perm.CODE_READ)


@router.post("/incidents/{ref}/actions", status_code=201, response_model=ApprovalOut)
async def request_action(
    ref: str,
    body: ActionRequest,
    request: Request,
    who: Annotated[Principal, Depends(require(Perm.ACTIONS_REQUEST))],
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    """An SRE asks for an action by hand. Idempotency-Key required; the same pending
    request (same incident + same exact arguments) is returned, never duplicated."""
    key = validate_key(idempotency_key, required=True)
    if body.investigation_id is not None:
        raise ValidationFailedError("investigation_id is set only by an investigation's proposal.")

    async def work(session: AsyncSession) -> tuple[int, dict[str, object]]:
        a, inc, created = await approvals.request(session, parse_incident_ref(ref), body, who,
                                                  source="manual")  # fmt: skip
        return (201 if created else 200), approvals.to_out(
            a, inc.key, code_reader=_code_reader(who)).model_dump(mode="json")  # fmt: skip

    result = await run_idempotent(
        _sessions(request), principal=who.subject, scope=f"POST /v1/incidents/{ref}/actions",
        key=key, body=body.model_dump(mode="json"), ttl=_settings(request).idempotency_ttl,
        work=work,
    )  # fmt: skip
    return _result_response(result, location=f"/v1/approvals/{result.body['id']}")


@router.post("/incidents/{ref}/actions/proposals", response_model=ApprovalOut)
async def propose_action(
    ref: str,
    body: ActionRequest,
    session: Session,
    who: Annotated[
        Principal,
        Depends(require_acting_user(Perm.ACTIONS_REQUEST, "actions:propose", service_only=True)),
    ],
) -> ApprovalOut:
    """Service-only (orchestrator): an investigation proposes an action FOR the user who
    started it (delegated token in X-On-Behalf-Of) - that user is the requester, so that user
    can never approve it. Idempotent per (investigation, exact args), whatever its status.
    Review: a USER bearer here could store a fake "code-written AI proposal" - refused, and the
    on-behalf-of token must be the investigation's delegated token."""
    if body.investigation_id is None:
        raise ValidationFailedError("A proposal names its investigation.")
    if who.delegation is None:
        raise ForbiddenError("A proposal carries the investigation's delegated token.")
    if who.delegation.investigation_id != body.investigation_id:
        raise ForbiddenError("The delegated token is bound to another investigation.")
    async with session.begin():
        a, inc, _ = await approvals.request(session, parse_incident_ref(ref), body, who,
                                            source="investigation")  # fmt: skip
        # the token is bound to ONE incident too (Phase 9 review lesson): raising here rolls
        # the whole request back - nothing is recorded for another incident
        if who.delegation.incident_id != inc.id:
            raise ForbiddenError("The delegated token is bound to another incident.")
        out = approvals.to_out(a, inc.key)
    return out


@router.get("/incidents/{ref}/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    ref: str,
    session: Session,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
) -> list[ApprovalOut]:
    """The decision record of an incident (every incident reader: who asked, who decided,
    why, what ran). Code-quoting parts of a recommendation need code:read."""
    inc, rows = await approvals.list_for_incident(session, parse_incident_ref(ref))
    return [approvals.to_out(a, inc.key, code_reader=_code_reader(who)) for a in rows]


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_pending_approvals(
    session: Session,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
    status: Annotated[str, Query(pattern="^PENDING$")] = "PENDING",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ApprovalOut]:
    """Phase 17b inbox: pending requests across incidents. The same readers and the same
    redaction as the per-incident list (an incident reader may see every incident)."""
    rows = await approvals.list_pending(session, limit)
    return [approvals.to_out(a, key, code_reader=_code_reader(who)) for a, key in rows]


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
async def get_approval(
    approval_id: UUID,
    session: Session,
    who: Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))],
) -> ApprovalOut:
    a, inc = await approvals.get(session, approval_id)
    return approvals.to_out(a, inc.key, code_reader=_code_reader(who))


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
async def decide(
    approval_id: UUID,
    body: DecisionIn,
    request: Request,
    who: Annotated[Principal, Depends(require(Perm.ACTIONS_APPROVE))],
) -> ApprovalOut:
    """INCIDENT_COMMANDER only, never the requester; reason mandatory. Executing an approved
    action is NOT done here (incident-service never executes): the orchestrator does it."""
    if who.is_service:
        raise ForbiddenError("A decision is a human's: user token only.")
    async with _sessions(request)() as session, session.begin():
        a, inc, expired = await approvals.decide(session, approval_id, body, who)
        out = approvals.to_out(a, inc.key, code_reader=_code_reader(who))
    if expired:  # recorded as EXPIRED above (committed) - THEN refused
        raise ConflictError("This request expired before a decision; request it again.")
    return out


@router.post("/approvals/{approval_id}/consume", response_model=ConsumeOut)
async def consume_approval(
    approval_id: UUID,
    body: ConsumeIn,
    session: Session,
    who: Annotated[
        Principal,
        Depends(
            require_acting_user(
                Perm.ACTIONS_APPROVE, "approvals:consume", allow_delegated=False, service_only=True
            )
        ),
    ],
) -> ConsumeOut:
    """Tool gateway only (a SERVICE bearer - an IC may not consume their own approval by hand,
    review H3), AS the acting user (never a delegated token: a background run can never
    execute a consequential action). Single use."""
    async with session.begin():
        return await approvals.consume(session, approval_id, body, who)


@router.post("/approvals/{approval_id}/outcome", response_model=ApprovalOut)
async def record_outcome(
    approval_id: UUID,
    body: OutcomeIn,
    session: Session,
    who: Annotated[Principal, Depends(require_scope("actions:record"))],
) -> ApprovalOut:
    """Orchestrator only: what the execution did (EXECUTED -> incident MITIGATED)."""
    async with session.begin():
        a, inc = await approvals.record_outcome(session, approval_id, body, who)
        return approvals.to_out(a, inc.key)


# ---------------------------------------------------------------- alerts (Phase 29b, ADR-044)


@router.post("/alerts", response_model=AlertsAccepted, tags=["alerts"])
async def receive_alerts(request: Request) -> AlertsAccepted:
    """Alertmanager webhook. NO JWT: the sender proves itself with an HMAC over timestamp +
    body (aeoi_security.webhook), checked here AND at the api edge. Order: configured (503)
    -> size (413) -> signature + freshness (401) -> payload (422) -> per-alert triage."""
    settings = _settings(request)
    secret = load_secret(settings.alert_secret_file)
    raw = await read_capped(request.stream())
    verify(
        secret,
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        signature=request.headers.get(SIGNATURE_HEADER),
        body=raw,
    )
    try:
        batch = AlertBatch.model_validate_json(raw)
    except ValidationError as exc:
        errors = [
            {"loc": [str(x) for x in e["loc"]], "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()[:20]
        ]
        raise ValidationFailedError(
            "Not an Alertmanager webhook payload (v4).", errors=errors
        ) from exc
    lookups = _BatchCatalog(request.app.state.catalog)
    results = [await _one_alert(request, lookups, a) for a in batch.alerts]
    return AlertsAccepted(received=len(batch.alerts), results=results)


class _BatchCatalog:
    """One catalog call per service per batch, and none after the catalog failed once: 20
    alerts x a 2 s timeout must not outlast the api's 5 s upstream timeout."""

    def __init__(self, catalog: CatalogClient) -> None:
        self._catalog = catalog
        self._seen: dict[str, tuple[CatalogEntry | None, str]] = {}
        self._down: str | None = None

    async def lookup(self, svc: str) -> tuple[CatalogEntry | None, str]:
        if svc in self._seen:
            return self._seen[svc]
        if self._down is not None:
            return None, f"{self._down} (earlier in this batch)"
        entry, note = await self._catalog.lookup(svc)
        if entry is None and note not in ("service not in the catalog", "catalog not configured"):
            self._down = note
        self._seen[svc] = (entry, note)
        return entry, note


async def _one_alert(request: Request, catalog: _BatchCatalog, alert: AlertIn) -> AlertResult:
    fp = alert.fingerprint
    if alert.status == "resolved":
        return AlertResult(fingerprint=fp, outcome="ignored_resolved",
                           reason="a resolved alert never closes an incident: a human decides")  # fmt: skip
    try:
        svc = service_of(alert)
    except AlertRejectedError as exc:
        return AlertResult(fingerprint=fp, outcome="rejected", reason=str(exc))
    entry, note = await catalog.lookup(svc)
    t = triage(alert, entry, note)

    async def work(session: AsyncSession) -> tuple[int, dict[str, object]]:
        incident = await service.create_incident(
            session, t.incident, ALERTS_PRINCIPAL, source="ALERT",
            detail={"alertname": alert.labels.get("alertname", "")[:100], "fingerprint": fp,
                    "rule": t.rule, "catalog": t.catalog, "blast_radius": t.blast_radius},
        )  # fmt: skip
        return 201, {"key": incident.key, "severity": incident.severity, "rule": t.rule}

    result = await run_idempotent(
        _sessions(request),
        principal=ALERTS_PRINCIPAL.subject,
        scope="POST /v1/alerts",
        key=dedupe_key(alert),
        body={"fingerprint": fp, "startsAt": alert.startsAt.isoformat()},
        ttl=DEDUPE_TTL,
        work=work,
    )
    return AlertResult(
        fingerprint=fp,
        outcome="duplicate" if result.replayed else "created",
        incident_key=str(result.body["key"]),
        severity=result.body["severity"],
        reason=str(result.body["rule"]),
    )
