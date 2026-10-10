"""Use cases. Each function runs inside a transaction owned by the caller."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aeoi_common.errors import (
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
    ValidationFailedError,
)
from aeoi_common.ids import uuid7
from aeoi_db.models.incident import (
    Evidence,
    Feedback,
    Hypothesis,
    HypothesisEvidence,
    Incident,
    IncidentEvent,
)
from aeoi_incident.domain import INVESTIGABLE, check_transition, decode_cursor, encode_cursor
from aeoi_incident.events import stage_event
from aeoi_models.api.agents import EvidenceBatch, EvidenceRetrievedPayload
from aeoi_models.api.hypotheses import (
    HypothesisBatch,
    HypothesisCreatedPayload,
    HypothesisEdge,
    HypothesisRecord,
)
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


async def evidence(
    session: AsyncSession, ref: UUID | int, kind: str | None, readable_kinds: list[str]
) -> list[Evidence]:
    """`readable_kinds` filters IN SQL (ADR-020): reading the incident is not reading its logs."""
    incident = await load(session, ref)
    stmt = select(Evidence).where(
        Evidence.incident_id == incident.id, Evidence.kind.in_(readable_kinds)
    )
    if kind:
        stmt = stmt.where(Evidence.kind == kind)
    return list((await session.scalars(stmt.order_by(Evidence.created_at).limit(1000))).all())


async def add_evidence(
    session: AsyncSession, ref: UUID | int, batch: EvidenceBatch, who: Principal
) -> tuple[int, int]:
    """Store evidence an agent retrieved (Phase 8). Idempotent on (incident, evidence_key):
    a redelivered batch inserts nothing and writes no second timeline entry or event.
    Evidence keys are tool-gateway ids (KIND-<tool_call>-<n>): globally unique, traceable."""
    incident = await load(session, ref)
    rows = [
        {
            "id": uuid7(),
            "incident_id": incident.id,
            "evidence_key": it.evidence_key,
            "kind": it.kind,
            "source_system": it.source_system,
            "title": it.title,
            "excerpt": it.excerpt,
            "content_sha256": it.content_sha256,
            "observed_at": it.observed_at,
            "retrieved_by": batch.agent,
            "tool_call_id": it.tool_call_id,
            "uri": it.uri,
        }
        for it in batch.items
    ]
    stmt = (
        insert(Evidence)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["incident_id", "evidence_key"])
        .returning(Evidence.evidence_key)
    )
    inserted = list((await session.scalars(stmt)).all())
    if inserted:
        actor = f"agent:{batch.agent}"
        session.add(
            _event(
                incident,
                source="AGENT",
                event_type="EvidenceRetrieved",
                summary=batch.summary,
                actor=actor,
                payload={
                    "evidence_keys": inserted[:50],
                    "count": len(inserted),
                    "investigation_id": str(batch.investigation_id or ""),
                    "via": who.actor,
                },
            )
        )
        stage_event(
            session,
            event_type=EventType.EVIDENCE_RETRIEVED,
            actor=actor,
            incident_id=incident.id,
            payload=EvidenceRetrievedPayload(
                incident_id=incident.id,
                agent=batch.agent,
                investigation_id=batch.investigation_id,
                evidence_keys=inserted[:200],
            ),
        )
    await session.flush()
    return len(rows), len(inserted)


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


# ------------------------------------------------------------------ hypotheses (Phase 11)
async def add_hypotheses(
    session: AsyncSession, ref: UUID | int, batch: HypothesisBatch, who: Principal
) -> tuple[int, int]:
    """Store an investigation's validated hypotheses + their evidence edges (ADR-021).
    Idempotent per (investigation_id, key). Every cited evidence key must already be stored
    for THIS incident (the evidence graph cannot point at nothing) - else 422, nothing written."""
    incident = await load(session, ref)
    keys = {k for it in batch.items for k in [*it.supports, *it.contradicts]}
    found = {
        r.evidence_key: r.id
        for r in (
            await session.execute(
                select(Evidence.evidence_key, Evidence.id).where(
                    Evidence.incident_id == incident.id, Evidence.evidence_key.in_(keys)
                )
            )
        ).all()
    }
    missing = sorted(keys - set(found))
    if missing:
        raise ValidationFailedError(
            f"hypotheses cite evidence not stored for this incident: {', '.join(missing[:5])}"
        )
    existing = set(
        (
            await session.scalars(
                select(Hypothesis.hypothesis_key).where(
                    Hypothesis.investigation_id == batch.investigation_id
                )
            )
        ).all()
    )
    new = [it for it in batch.items if it.key not in existing]
    for it in new:
        hid = uuid7()
        session.add(
            Hypothesis(
                id=hid, incident_id=incident.id, statement=it.statement,
                confidence=it.confidence.value, status=it.status, rank=it.rank,
                produced_by=it.produced_by, investigation_id=batch.investigation_id,
                hypothesis_key=it.key, detail=dict(it.detail),
            )
        )  # fmt: skip
        await session.flush()
        edges = {found[k]: "SUPPORTS" for k in it.supports}
        edges |= {found[k]: "CONTRADICTS" for k in it.contradicts if found[k] not in edges}
        for eid, stance in edges.items():
            session.add(HypothesisEvidence(hypothesis_id=hid, evidence_id=eid, stance=stance))
    if new:
        actor = f"agent:{new[0].produced_by}"
        hypos = [it for it in new if it.status != "REJECTED"]
        top = min(hypos, key=lambda it: it.rank) if hypos else None
        session.add(
            _event(
                incident, source="AGENT", event_type="HypothesisCreated", actor=actor,
                summary=(f"{len(hypos)} hypotheses; top ({top.confidence.value}): "
                         f"{top.statement}")[:300] if top else f"{len(new)} cause(s) ruled out",
                payload={"investigation_id": str(batch.investigation_id),
                         "keys": [it.key for it in new], "via": who.actor},
            )
        )  # fmt: skip
        payload = HypothesisCreatedPayload(
            incident_id=incident.id, investigation_id=batch.investigation_id,
            keys=[it.key for it in new],
            challenged=[it.key for it in new if it.status == "CHALLENGED"],
        )  # fmt: skip
        stage_event(session, event_type=EventType.HYPOTHESIS_CREATED, actor=actor,
                    incident_id=incident.id, payload=payload)  # fmt: skip
    return len(batch.items), len(new)


MODEL_TEXT = ("explanation", "critic_missing", "critic_disputed", "refinement")
REDACTED_TEXT = "[redacted: needs read access to all of its evidence]"


async def hypotheses(
    session: AsyncSession, ref: UUID | int, investigation_id: UUID | None, readable: list[str]
) -> list[HypothesisRecord]:
    """Latest investigation's hypotheses by default. CODE-written statements are conclusions
    and are shown to every incident reader; the EVIDENCE edges are filtered by kind like GET
    evidence. MODEL-written text (the ranker's explanation, the critic's 'missing', a critic
    alternative's statement) can quote evidence, so a reader who lacks a permission for ANY
    of the hypothesis' evidence kinds gets it redacted (review finding: the trace endpoint
    already hid exactly this text)."""
    incident = await load(session, ref)
    if investigation_id is None:
        investigation_id = await session.scalar(
            select(Hypothesis.investigation_id)
            .where(Hypothesis.incident_id == incident.id, Hypothesis.investigation_id.is_not(None))
            .order_by(Hypothesis.created_at.desc())
            .limit(1)
        )
        if investigation_id is None:
            return []
    rows = list(
        (
            await session.scalars(
                select(Hypothesis)
                .where(
                    Hypothesis.incident_id == incident.id,
                    Hypothesis.investigation_id == investigation_id,
                )
                .order_by(Hypothesis.status == "REJECTED", Hypothesis.rank)
            )
        ).all()
    )
    edges = (
        await session.execute(
            select(HypothesisEvidence.hypothesis_id, HypothesisEvidence.stance,
                   Evidence.evidence_key, Evidence.kind)
            .join(Evidence, Evidence.id == HypothesisEvidence.evidence_id)
            .where(HypothesisEvidence.hypothesis_id.in_([h.id for h in rows]))
        )
    ).all()  # fmt: skip
    out = []
    for h in rows:
        mine = [e for e in edges if e.hypothesis_id == h.id]
        shown = [e for e in mine if e.kind in readable]
        detail = dict(h.detail or {})
        statement = h.statement
        redacted = len(shown) < len(mine)
        if redacted:
            for k in MODEL_TEXT:
                if detail.get(k):
                    detail[k] = REDACTED_TEXT
            if detail.get("origin") == "critic":
                statement = f"Alternative cause proposed by the critic ({REDACTED_TEXT})"
        out.append(
            HypothesisRecord(
                id=h.id, investigation_id=h.investigation_id, key=h.hypothesis_key,
                statement=statement, confidence=h.confidence, status=h.status, rank=h.rank,
                produced_by=h.produced_by, detail=detail, created_at=h.created_at,
                edges=[HypothesisEdge(evidence_key=e.evidence_key, kind=e.kind,
                                      stance=e.stance) for e in shown],
                hidden_edges=len(mine) - len(shown),
            )
        )  # fmt: skip
    return out
