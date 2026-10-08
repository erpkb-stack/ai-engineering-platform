"""Orchestrator HTTP API (Phase 9, ADR-019). Callers are USERS (via the api gateway).

Start: the user's own token is used here, inside this request, three times - read the
incident, mark it INVESTIGATING, exchange it for a delegation grant - and then dropped.
Reads: every read first asks incident-service for the incident AS THE CALLER, so the
orchestrator never shows an investigation of an incident the caller may not see (404).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response

from aeoi_common.errors import (
    AEOIError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamUnavailableError,
)
from aeoi_common.ids import uuid7
from aeoi_db.models.orchestrator import Investigation
from aeoi_models.api.incidents import InvestigationAccepted
from aeoi_models.api.investigations import (
    CancelInvestigation,
    ExecutionTraceOut,
    InvestigationOut,
    InvestigationTrace,
    StartInvestigation,
    TaskOut,
)
from aeoi_orchestrator.clients import CallError, IncidentClient
from aeoi_orchestrator.config import Settings
from aeoi_orchestrator.delegation import DelegationClient, DelegationError
from aeoi_orchestrator.engine import Engine
from aeoi_orchestrator.store import AlreadyRunningError, Store
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web import require

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["investigations"])
Runner = Annotated[Principal, Depends(require(Perm.INVESTIGATIONS_RUN))]
Reader = Annotated[Principal, Depends(require(Perm.INCIDENTS_READ))]
IdemKey = Annotated[
    str | None,
    # same rule as incident-service (it gets the same key): checked HERE, before any side effect
    Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9._:-]{8,128}$"),
]


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _state(request: Request) -> tuple[Settings, Store, Engine, IncidentClient, DelegationClient]:
    st = request.app.state
    return st.settings, st.store, st.engine, st.incidents, st.delegation


def _upstream(exc: CallError) -> AEOIError:
    """Pass the incident-service decision through; never turn a 403/404 into a 500."""
    if exc.status == 404:
        return NotFoundError(str(exc))
    if exc.status == 403:
        return ForbiddenError(str(exc))
    if exc.status == 409:
        return ConflictError(str(exc))
    if exc.status in (400, 422):
        return BadRequestError(str(exc))
    return UpstreamUnavailableError(str(exc))


async def _visible(request: Request, incident_id: UUID) -> dict[str, Any]:
    """The incident as the caller may see it, or 404 (also when they may not see it)."""
    _, _, _, incidents, _ = _state(request)
    try:
        return await incidents.get_as_user(str(incident_id), _bearer(request))
    except CallError as exc:
        if exc.status in (403, 404):
            raise NotFoundError("Investigation not found.") from exc
        raise _upstream(exc) from exc


async def _out(request: Request, inv: Investigation) -> InvestigationOut:
    _, store, engine, _, _ = _state(request)
    plan = dict(inv.plan or {})
    plan.pop("start_key", None)
    graph_input = plan.pop("input", {})
    return InvestigationOut(
        investigation_id=inv.id,
        incident_id=inv.incident_id,
        status=inv.status,
        requested_by=inv.requested_by,
        started_at=inv.started_at,
        finished_at=inv.finished_at,
        deadline_at=inv.deadline_at,
        spent_usd=inv.spent_usd,
        error=inv.error or plan.get("error"),
        plan={**plan, "window": graph_input.get("window"), "route": graph_input.get("llm_route")},
        tasks=[TaskOut(**t) for t in await store.tasks(inv.id)],
        evidence_inserted=plan.get("evidence_inserted"),
        graph_next=await engine.next_nodes(inv.id) if inv.status == "RUNNING" else [],
    )


@router.post("/investigations", status_code=202, response_model=InvestigationAccepted)
async def start_investigation(
    body: StartInvestigation,
    request: Request,
    response: Response,
    who: Runner,
    idempotency_key: IdemKey = None,
) -> InvestigationAccepted:
    """Order matters (review findings): every check that can refuse runs BEFORE anything is
    changed elsewhere - read incident (as the user) -> one-RUNNING insert -> token exchange
    (directory re-check) -> only then mark the incident INVESTIGATING. A start that fails
    clears its Idempotency-Key, so a retry with the same key really retries."""
    settings, store, engine, incidents, delegation = _state(request)
    if not idempotency_key:
        raise BadRequestError(
            "Idempotency-Key header is required (8-128 chars of [A-Za-z0-9._:-])."
        )
    user_token = _bearer(request)
    key = idempotency_key
    try:
        incident = await incidents.get_as_user(body.incident, user_token)
    except CallError as exc:
        raise _upstream(exc) from exc
    incident_id = UUID(incident["id"])
    replay = await store.find_by_start_key(incident_id, who.actor, key)
    if replay is not None:  # same user, same key, a start that got through: the same run
        response.headers["Location"] = f"/v1/investigations/{replay.id}"
        response.headers["Idempotent-Replayed"] = "true"
        return InvestigationAccepted(
            request_id=UUID(str(replay.plan.get("request_id"))),
            incident_id=incident_id,
            status=replay.status,
            investigation_id=replay.id,
        )
    services = list(incident.get("affected_services") or [])[:3]
    if not services:
        raise BadRequestError(
            f"{incident['key']} has no affected_services: nothing to investigate."
        )

    detected = datetime.fromisoformat(incident["detected_at"])
    start = body.start or detected - timedelta(minutes=settings.window_before_min)
    end = body.end or detected + timedelta(minutes=settings.window_after_min)
    investigation_id = uuid7()
    graph_input: dict[str, Any] = {
        "investigation_id": str(investigation_id),
        "incident": {
            "id": str(incident_id),
            "key": incident["key"],
            "services": services,
            "detected_at": incident["detected_at"],
        },
        "window": {
            "start": start.astimezone(UTC).isoformat(),
            "end": end.astimezone(UTC).isoformat(),
        },
        "llm_route": body.llm_route,
        "llm_cache": body.llm_cache,
        "results": [],
    }
    try:
        await store.create_investigation(
            investigation_id,
            incident_id,
            who.actor,
            settings.budget_usd,
            settings.investigation_deadline_s,
            plan={"phase": 9, "agents": ["log_analysis"], "start_key": key},
        )
    except AlreadyRunningError as exc:
        raise ConflictError(
            f"Investigation {exc.investigation_id} of this incident is already RUNNING "
            f"(requested by {exc.requested_by}). One at a time per incident."
        ) from exc

    async def abort(error: str, grant_id: UUID | None) -> None:
        """Start failed after the row exists: no live grant, no RUNNING row, key freed."""
        if grant_id is not None:
            await delegation.revoke(grant_id, f"start failed: {error[:120]}")
        await store.finish(investigation_id, "FAILED", error)
        await store.note(investigation_id, start_key=None)

    try:
        grant = await delegation.create(user_token, investigation_id, incident_id)
    except DelegationError as exc:
        await abort(f"delegation refused: {exc}", None)
        if exc.final:
            raise ForbiddenError(f"Cannot act for you in this investigation: {exc}") from exc
        raise UpstreamUnavailableError(f"Token exchange unavailable: {exc}") from exc
    try:
        accepted = await incidents.mark_investigating(str(incident_id), user_token, key)
    except CallError as exc:
        await abort(f"incident-service refused the investigation: {exc}", grant.grant_id)
        raise _upstream(exc) from exc
    graph_input["grant_id"] = str(grant.grant_id)
    try:
        status = await store.attach_grant(
            investigation_id, grant.grant_id, graph_input, str(accepted["request_id"])
        )
    except Exception:
        await delegation.revoke(grant.grant_id, "start failed: could not record the grant")
        raise
    if status != "RUNNING":  # cancelled / failed by someone else while we were starting
        await delegation.revoke(grant.grant_id, f"investigation {status} during start")
        raise ConflictError(f"Investigation {investigation_id} was {status} while starting.")
    engine.launch(investigation_id, graph_input)
    log.info(
        "investigation_started",
        investigation_id=str(investigation_id),
        incident=incident["key"],
        by=who.actor,
        grant_id=str(grant.grant_id),
    )
    response.headers["Location"] = f"/v1/investigations/{investigation_id}"
    return InvestigationAccepted(
        request_id=UUID(str(accepted["request_id"])),
        incident_id=incident_id,
        status="RUNNING",
        investigation_id=investigation_id,
    )


async def _load(request: Request, investigation_id: UUID) -> Investigation:
    _, store, _, _, _ = _state(request)
    inv = await store.get(investigation_id)
    if inv is None:
        raise NotFoundError("Investigation not found.")
    await _visible(request, inv.incident_id)
    return inv


@router.get("/investigations/{investigation_id}", response_model=InvestigationOut)
async def get_investigation(
    investigation_id: UUID, request: Request, _: Reader
) -> InvestigationOut:
    return await _out(request, await _load(request, investigation_id))


# what an agent read, by agent: seeing its prompts/output needs the same permission
# (review finding: the trace let incidents:read-only roles - MANAGER, ADMIN - read log lines)
AGENT_CONTENT_PERMS: dict[str, Perm] = {"log_analysis": Perm.LOGS_READ}


@router.get("/investigations/{investigation_id}/trace", response_model=InvestigationTrace)
async def get_trace(investigation_id: UUID, request: Request, who: Reader) -> InvestigationTrace:
    inv = await _load(request, investigation_id)
    store: Store = request.app.state.store
    out = []
    for e in await store.trace(inv.id):
        needed = AGENT_CONTENT_PERMS.get(e["agent"], Perm.LOGS_READ)  # unknown agent: strictest
        if not who.has(needed):
            e = {**e, "messages": [], "output": {}, "redacted": True}
        out.append(ExecutionTraceOut(**e))
    return InvestigationTrace(investigation_id=inv.id, executions=out)


@router.get("/incidents/{ref}/investigations", response_model=list[InvestigationOut])
async def list_investigations(ref: str, request: Request, _: Reader) -> list[InvestigationOut]:
    store: Store = request.app.state.store
    incidents: IncidentClient = request.app.state.incidents
    try:
        incident = await incidents.get_as_user(ref, _bearer(request))
    except CallError as exc:
        raise _upstream(exc) from exc
    return [await _out(request, inv) for inv in await store.list_for_incident(UUID(incident["id"]))]


def _may_control(who: Principal, inv: Investigation) -> None:
    if inv.requested_by != who.actor and not who.has(Perm.INCIDENTS_WRITE):
        raise ForbiddenError("Only the requester or an incident commander can do this.")


@router.post("/investigations/{investigation_id}/cancel", response_model=InvestigationOut)
async def cancel_investigation(
    investigation_id: UUID, body: CancelInvestigation, request: Request, who: Runner
) -> InvestigationOut:
    _, _, engine, _, _ = _state(request)
    inv = await _load(request, investigation_id)
    _may_control(who, inv)
    if inv.status != "RUNNING":
        raise ConflictError(f"Investigation is {inv.status}, not RUNNING.")
    await engine.cancel(investigation_id, f"{body.reason} (by {who.actor})")
    return await _out(request, await _load(request, investigation_id))


@router.post("/investigations/{investigation_id}/resume", response_model=InvestigationOut)
async def resume_investigation(
    investigation_id: UUID, request: Request, who: Runner
) -> InvestigationOut:
    """Operator aid: relaunch a RUNNING investigation this process is not running (the
    startup scan does the same for all of them)."""
    _, _, engine, _, _ = _state(request)
    inv = await _load(request, investigation_id)
    _may_control(who, inv)
    if inv.status != "RUNNING":
        raise ConflictError(f"Investigation is {inv.status}; only RUNNING ones resume.")
    if not engine.running(investigation_id):
        await engine.resume_pending(only=investigation_id)
    return await _out(request, await _load(request, investigation_id))
