"""Use cases. Each function runs inside a transaction owned by the caller."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aeoi_common.errors import (
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
    ValidationFailedError,
)
from aeoi_common.ids import uuid7
from aeoi_db.models.incident import Evidence, Feedback, Incident, IncidentEvent
from aeoi_incident.domain import INVESTIGABLE, check_transition, decode_cursor, encode_cursor
from aeoi_incident.events import stage_event
from aeoi_models.api.incidents import (
    FeedbackCreate,
    IncidentCreate,
    IncidentCreatedPayload,
    IncidentOut,
    IncidentPatch,
    IncidentStatus,
    IncidentUpdatedPayload,
    InvestigationRequestedPayload,
)
from aeoi_models.events import EventType
from aeoi_security.auth import Principal


def to_out(incident: Incident) -> IncidentOut:
    return IncidentOut.model_validate(incident)


async def load(session: AsyncSession, ref: UUID | int, *, for_update: bool = False) -> Incident:
    stmt = select(Incident).where(
        Incident.id == ref if isinstance(ref, UUID) else Incident.number == ref
    )
    if for_update:
        stmt = stmt.with_for_update()
    incident = (await session.scalars(stmt)).one_or_none()
    if incident is None:
        raise NotFoundError("Incident not found.")
    return incident


def _event(
    incident: Incident,
    *,
    source: str,
    event_type: str,
    summary: str,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> IncidentEvent:
    return IncidentEvent(
        id=uuid7(),
        incident_id=incident.id,
        occurred_at=datetime.now(UTC),
        source=source,
        event_type=event_type,
        summary=summary,
        actor=actor,
        payload=payload or {},
    )


async def create_incident(session: AsyncSession, data: IncidentCreate, who: Principal) -> Incident:
    incident = Incident(
        id=uuid7(),
        title=data.title,
        description=data.description,
        severity=data.severity.value,
        status=IncidentStatus.OPEN.value,
        affected_services=list(data.affected_services),
        detected_at=data.detected_at or datetime.now(UTC),
        created_by=who.actor,
    )
    session.add(incident)
    await session.flush()  # get number/defaults from the DB
    await session.refresh(incident)
    session.add(
        _event(
            incident,
            source="HUMAN",
            event_type="IncidentCreated",
            summary=f"{incident.key} declared ({incident.severity}) by {who.name or who.actor}",
            actor=who.actor,
        )
    )
    stage_event(
        session,
        event_type=EventType.INCIDENT_CREATED,
        actor=who.actor,
        incident_id=incident.id,
        payload=IncidentCreatedPayload(
            incident_id=incident.id,
            key=incident.key,
            title=incident.title,
            severity=data.severity,
            affected_services=incident.affected_services,
            detected_at=incident.detected_at,
        ),
    )
    return incident


async def list_incidents(
    session: AsyncSession,
    *,
    status: IncidentStatus | None,
    severity: str | None,
    service: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Incident], str | None]:
    stmt = select(Incident)
    if status:
        stmt = stmt.where(Incident.status == status.value)
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    if service:
        stmt = stmt.where(Incident.affected_services.contains([service]))
    if cursor:
        c_at, c_id = decode_cursor(cursor)
        # keyset: strictly "older than" the last row seen (stable under concurrent inserts)
        stmt = stmt.where(
            or_(Incident.created_at < c_at, and_(Incident.created_at == c_at, Incident.id < c_id))
        )
    rows = list(
        (
            await session.scalars(
                stmt.order_by(Incident.created_at.desc(), Incident.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    next_cursor = (
        encode_cursor(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    )
    return rows[:limit], next_cursor


async def update_incident(
    session: AsyncSession,
    ref: UUID | int,
    patch: IncidentPatch,
    expected_version: int,
    who: Principal,
) -> Incident:
    incident = await load(session, ref, for_update=True)
    if incident.version != expected_version:
        raise PreconditionFailedError(
            f"Incident changed since you read it (your version {expected_version}, "
            f"current {incident.version}). Reload and retry."
        )
    changes: dict[str, list[str]] = {}
    if patch.status is not None and patch.status.value != incident.status:
        current = IncidentStatus(incident.status)
        check_transition(current, patch.status)
        changes["status"] = [incident.status, patch.status.value]
        incident.status = patch.status.value
        if (
            patch.status in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
            and incident.resolved_at is None
        ):
            incident.resolved_at = datetime.now(UTC)
        if current == IncidentStatus.RESOLVED and patch.status == IncidentStatus.INVESTIGATING:
            incident.resolved_at = None  # reopened
    if patch.severity is not None and patch.severity.value != incident.severity:
        changes["severity"] = [incident.severity, patch.severity.value]
        incident.severity = patch.severity.value
    if not changes:
        raise ValidationFailedError("Nothing to change: status/severity equal the current values.")
    incident.version += 1
    summary = ", ".join(f"{k} {a} → {b}" for k, (a, b) in changes.items())
    session.add(
        _event(
            incident,
            source="HUMAN",
            event_type="IncidentUpdated",
            summary=f"{summary}: {patch.reason}",
            actor=who.actor,
            payload={"changes": changes},
        )
    )
    stage_event(
        session,
        event_type=EventType.INCIDENT_UPDATED,
        actor=who.actor,
        incident_id=incident.id,
        payload=IncidentUpdatedPayload(
            incident_id=incident.id,
            key=incident.key,
            changes=changes,
            reason=patch.reason,
            version=incident.version,
        ),
    )
    await session.flush()
    await session.refresh(incident)
    return incident


async def request_investigation(
    session: AsyncSession, ref: UUID | int, who: Principal
) -> tuple[Incident, UUID]:
    incident = await load(session, ref, for_update=True)
    current = IncidentStatus(incident.status)
    if current not in INVESTIGABLE:
        raise ConflictError(f"Cannot investigate an incident in status {current}.")
    if current != IncidentStatus.INVESTIGATING:
        check_transition(current, IncidentStatus.INVESTIGATING)
        incident.status = IncidentStatus.INVESTIGATING.value
        if current == IncidentStatus.RESOLVED:
            incident.resolved_at = None
        incident.version += 1
    request_id = uuid7()
    session.add(
        _event(
            incident,
            source="HUMAN",
            event_type="InvestigationRequested",
            summary=f"AI investigation requested by {who.name or who.actor}",
            actor=who.actor,
            payload={"request_id": str(request_id)},
        )
    )
    stage_event(
        session,
        event_type=EventType.INVESTIGATION_REQUESTED,
        actor=who.actor,
        incident_id=incident.id,
        payload=InvestigationRequestedPayload(
            request_id=request_id, incident_id=incident.id, key=incident.key, requested_by=who.actor
        ),
    )
    await session.flush()
    return incident, request_id


async def timeline(session: AsyncSession, ref: UUID | int) -> list[IncidentEvent]:
    incident = await load(session, ref)
    stmt = (
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident.id)
        .order_by(IncidentEvent.occurred_at, IncidentEvent.id)
        .limit(1000)
    )
    return list((await session.scalars(stmt)).all())


async def evidence(session: AsyncSession, ref: UUID | int, kind: str | None) -> list[Evidence]:
    incident = await load(session, ref)
    stmt = select(Evidence).where(Evidence.incident_id == incident.id)
    if kind:
        stmt = stmt.where(Evidence.kind == kind)
    return list((await session.scalars(stmt.order_by(Evidence.created_at).limit(1000))).all())


async def create_feedback(session: AsyncSession, data: FeedbackCreate, who: Principal) -> Feedback:
    if data.incident_id is not None:
        await load(session, data.incident_id)
    row = Feedback(
        id=uuid7(),
        incident_id=data.incident_id,
        target_type=data.target_type.value,
        target_id=data.target_id,
        user_id=who.user_id,
        rating=data.rating,
        comment=data.comment,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row
