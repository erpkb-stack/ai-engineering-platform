"""Phase 16 (ADR-026): the approval record - every rule that makes an approval mean something.

Use cases (the caller owns the transaction; every change = row + incident_events row + outbox
event, in ONE transaction, like the rest of incident-service):

  request   an SRE (by hand) or an investigation (for the user who started it) asks for ONE
            action with ONE exact argument set. One pending request per (incident, args).
            Expires after 30 minutes. Incident -> AWAITING_REVIEW.
  decide    an INCIDENT_COMMANDER who did NOT request it: APPROVED / REJECTED / MORE_INFO,
            reason mandatory. A late decision marks the request EXPIRED instead.
  consume   the tool gateway, AS the acting user, just before executing: this approval, by
            THIS approver, for THIS action + args hash + incident, APPROVED, not expired, not
            used -> used. A retry of the same execution (consumed, no outcome yet) is allowed.
  outcome   the orchestrator records what the execution did. EXECUTED -> incident MITIGATED.

incident-service never executes anything (CLAUDE.md): it is the record the gateway asks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aeoi_common.errors import ConflictError, ForbiddenError, NotFoundError
from aeoi_common.ids import uuid7
from aeoi_db.models.incident import Approval, Incident
from aeoi_incident.domain import TRANSITIONS
from aeoi_incident.events import stage_event
from aeoi_incident.service import _event, load
from aeoi_models.api.approvals import (
    APPROVAL_TTL_MINUTES,
    ActionRequest,
    ApprovalEventPayload,
    ApprovalOut,
    ConsumeIn,
    ConsumeOut,
    DecisionIn,
    OutcomeIn,
    args_sha256,
)
from aeoi_models.api.incidents import IncidentStatus
from aeoi_models.events import EventType
from aeoi_security.auth import Principal

# recommendation keys that quote code (function names, flags, commits): code:read readers only
CODE_KEYS = ("way_back",)


def status_of(a: Approval, now: datetime | None = None) -> str:
    if a.outcome:
        return a.outcome
    if a.decision:
        return a.decision
    return "EXPIRED" if a.expires_at <= (now or datetime.now(UTC)) else "PENDING"


def to_out(a: Approval, incident_key: str, *, code_reader: bool = True) -> ApprovalOut:
    rec = dict(a.recommendation or {})
    if not code_reader:
        for k in CODE_KEYS:
            if k in rec:
                rec[k] = "[redacted: needs code:read]"
    detail = dict(a.outcome_detail or {})
    if not code_reader and "result" in detail:  # commits of the CD record (review L1)
        detail["result"] = "[redacted: needs code:read]"
    return ApprovalOut(
        id=a.id, incident_id=a.incident_id, incident_key=incident_key, action=a.action,  # type: ignore[arg-type]
        action_args=a.action_args, args_sha256=a.args_sha256, recommendation=rec,
        evidence_keys=list(a.evidence_keys), source=a.source,  # type: ignore[arg-type]
        investigation_id=a.investigation_id, requested_by=a.requested_by,
        requested_at=a.requested_at, expires_at=a.expires_at, status=status_of(a),  # type: ignore[arg-type]
        decided_by=a.decided_by, decider_role=a.decider_role, decided_at=a.decided_at,
        reason=a.reason, consumed_at=a.consumed_at, outcome=a.outcome,
        outcome_detail=detail, tool_call_id=a.tool_call_id,
    )  # fmt: skip


def _stage(session: AsyncSession, incident: Incident, a: Approval, event: EventType,
           actor: str, summary: str, source: str = "HUMAN") -> None:  # fmt: skip
    session.add(_event(incident, source=source, event_type=str(event), summary=summary[:500],
                       actor=actor, payload={"approval_id": str(a.id), "action": a.action,
                                             "args": a.action_args, "status": status_of(a)}))  # fmt: skip
    stage_event(session, event_type=event, actor=actor, incident_id=incident.id,
                payload=ApprovalEventPayload(approval_id=a.id, incident_id=incident.id,
                                             key=incident.key, action=a.action,
                                             status=status_of(a), actor=actor))  # fmt: skip


def _move(incident: Incident, target: IncidentStatus) -> None:
    current = IncidentStatus(incident.status)
    if current != target and target in TRANSITIONS.get(current, frozenset()):
        incident.status = target.value
        incident.version += 1


async def _locked(session: AsyncSession, approval_id: UUID) -> tuple[Approval, Incident]:
    """ONE lock order everywhere (review M6): the incident first, then the approval -
    `request` does the same, so a request and a decision cannot deadlock."""
    incident_id = await session.scalar(
        select(Approval.incident_id).where(Approval.id == approval_id)
    )
    if incident_id is None:
        raise NotFoundError("Approval not found.")
    incident = await load(session, incident_id, for_update=True)
    a = (await session.scalars(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )).one()  # fmt: skip
    return a, incident


# a rollback on a resolved or closed incident would change production behind a closed record
# (review M5): request it again after reopening
CLOSED = (IncidentStatus.RESOLVED.value, IncidentStatus.CLOSED.value)


def _open(incident: Incident) -> None:
    if incident.status in CLOSED:
        raise ConflictError(f"Incident {incident.key} is {incident.status}: reopen it first.")


async def _settle(session: AsyncSession, incident: Incident, a: Approval, now: datetime) -> None:
    """A request closed without mitigating (rejected, more info, expired, failed): leave
    AWAITING_REVIEW if nothing else waits for a decision - never a review state with nothing to
    review (Phase 16 review). Back to INVESTIGATING: the incident is still not mitigated."""
    if incident.status != IncidentStatus.AWAITING_REVIEW.value:
        return
    waiting = or_(Approval.decision.is_(None),  # undecided, or approved and not run yet
                  and_(Approval.decision == "APPROVED", Approval.outcome.is_(None)))  # fmt: skip
    others = (await session.scalars(select(Approval.id).where(
        Approval.incident_id == incident.id, waiting,
        Approval.id != a.id, Approval.expires_at > now))).first()  # fmt: skip
    if others is None:
        _move(incident, IncidentStatus.INVESTIGATING)


async def _expire(session: AsyncSession, incident: Incident, a: Approval, now: datetime) -> None:
    a.decision, a.decided_at, a.reason = "EXPIRED", now, "expired before a decision"
    await _settle(session, incident, a, now)
    _stage(session, incident, a, EventType.ACTION_EXPIRED, "service:incident-service",
           f"Action {a.action} expired without a decision", source="SYSTEM")  # fmt: skip


async def request(
    session: AsyncSession, ref: UUID | int, req: ActionRequest, who: Principal, *, source: str
) -> tuple[Approval, Incident, bool]:
    """(approval, incident, created). The same pending request again is NOT a second one."""
    incident = await load(session, ref, for_update=True)
    _open(incident)
    args = req.args.model_dump(mode="json")
    digest = args_sha256(req.action, args)
    now = datetime.now(UTC)
    if source == "investigation" and req.investigation_id is not None:
        # an investigation proposes a given action ONCE, whatever happened to it since: a
        # resumed graph re-running its propose step must not reopen a decided request
        earlier = (await session.scalars(
            select(Approval).where(Approval.incident_id == incident.id,
                                   Approval.investigation_id == req.investigation_id,
                                   Approval.args_sha256 == digest)
        )).first()  # fmt: skip
        if earlier is not None:
            return earlier, incident, False
    pending = (await session.scalars(
        select(Approval).where(Approval.incident_id == incident.id,
                               Approval.args_sha256 == digest,
                               Approval.decision.is_(None)).with_for_update()
    )).one_or_none()  # fmt: skip
    if pending is not None:
        if pending.expires_at > now:
            return pending, incident, False
        await _expire(session, incident, pending, now)
        await session.flush()  # the partial unique index must see it closed first
    # one live request per TARGET (review M2): the args hash includes the free-text reason, so
    # two reasons for the same rollback would be two requests - and two approvals racing
    same_target = (await session.scalars(
        select(Approval).where(
            Approval.incident_id == incident.id, Approval.action == req.action,
            Approval.action_args["deploy_key"].astext == args["deploy_key"],
            Approval.expires_at > now,
            or_(Approval.decision.is_(None),
                and_(Approval.decision == "APPROVED", Approval.outcome.is_(None))))
    )).first()  # fmt: skip
    if same_target is not None:
        if source == "investigation":  # the same rollback is already asked for: that one
            return same_target, incident, False
        raise ConflictError(f"A {req.action} of {args['deploy_key']} is already "
                            f"{status_of(same_target, now)} ({same_target.id}).")  # fmt: skip
    a = Approval(
        id=uuid7(), incident_id=incident.id, action=req.action, action_args=args,
        args_sha256=digest, recommendation=req.recommendation,
        evidence_keys=list(req.evidence_keys), requested_by=who.actor, requested_at=now,
        expires_at=now + timedelta(minutes=APPROVAL_TTL_MINUTES), source=source,
        investigation_id=req.investigation_id,
    )  # fmt: skip
    session.add(a)
    _move(incident, IncidentStatus.AWAITING_REVIEW)
    await session.flush()
    _stage(session, incident, a, EventType.HUMAN_REVIEW_REQUIRED, who.actor,
           f"Approval requested ({source}): {req.action} {args.get('deploy_key', '')} - "
           f"by {who.name or who.actor}", source="AGENT" if source == "investigation" else "HUMAN")  # fmt: skip
    return a, incident, True


async def decide(
    session: AsyncSession, approval_id: UUID, d: DecisionIn, who: Principal
) -> tuple[Approval, Incident, bool]:
    """(approval, incident, expired_now). expired_now: the request was marked EXPIRED in this
    transaction - the caller commits that and THEN reports 409 (so the expiry is recorded)."""
    a, incident = await _locked(session, approval_id)
    if a.requested_by == who.actor:
        raise ForbiddenError("You requested this action: another incident commander must decide "
                             "(separation of duties).")  # fmt: skip
    if a.decision is not None:
        raise ConflictError(f"This request is already decided ({a.decision}).")
    now = datetime.now(UTC)
    if a.expires_at <= now:
        await _expire(session, incident, a, now)
        return a, incident, True
    if d.decision == "APPROVED":
        _open(incident)
    a.decision, a.decided_by, a.decided_at, a.reason = d.decision, who.user_id, now, d.reason
    a.decider_role = ",".join(sorted(who.roles))[:40] or None
    event = {"APPROVED": EventType.HUMAN_APPROVED, "REJECTED": EventType.HUMAN_REJECTED,
             "MORE_INFO": EventType.HUMAN_MORE_INFO}[d.decision]  # fmt: skip
    if d.decision != "APPROVED":
        await _settle(session, incident, a, now)
    _stage(session, incident, a, event, who.actor,
           f"{d.decision.replace('_', ' ').title()} by {who.name or who.actor}: {d.reason}")  # fmt: skip
    return a, incident, False


async def consume(
    session: AsyncSession, approval_id: UUID, body: ConsumeIn, who: Principal
) -> ConsumeOut:
    """The gateway's verification. Every mismatch is a refusal with a reason; the approval is
    never used for anything but exactly what was approved, by whom it was approved."""
    a, incident = await _locked(session, approval_id)
    if a.decision != "APPROVED":
        raise ConflictError(f"Approval is not approved (status {status_of(a)}).")
    if a.decided_by != who.user_id:
        raise ForbiddenError("Only the approver can execute this approval.")
    if a.action != body.action or a.args_sha256 != body.args_sha256:
        raise ForbiddenError("This approval was given for a different action or arguments.")
    if a.incident_id != body.incident_id:
        raise ForbiddenError("This approval belongs to another incident.")
    _open(incident)
    now = datetime.now(UTC)
    if a.consumed_at is not None and a.outcome is not None:
        raise ConflictError("This approval was already used.")
    # also for a resume (review H3): an approval never acts after it expired, however it was
    # left - the gateway still answers "already executed" if it did run
    if a.expires_at <= now:
        raise ConflictError("This approval has expired.")
    if a.consumed_at is not None:  # a retry of the same execution: the gateway decides (intent)
        return ConsumeOut(approval_id=a.id, consumed_at=a.consumed_at, resumed=True)
    a.consumed_at = now
    return ConsumeOut(approval_id=a.id, consumed_at=now)


async def record_outcome(
    session: AsyncSession, approval_id: UUID, body: OutcomeIn, who: Principal
) -> tuple[Approval, Incident]:
    a, incident = await _locked(session, approval_id)
    if a.consumed_at is None:
        raise ConflictError("Nothing was executed for this approval (not consumed).")
    if a.outcome is not None:
        if a.outcome == body.outcome and a.tool_call_id == body.tool_call_id:
            return a, incident  # the same report again: idempotent
        raise ConflictError(f"Outcome already recorded ({a.outcome}).")
    a.outcome, a.tool_call_id = body.outcome, body.tool_call_id
    a.outcome_detail, a.executed_at = body.detail, datetime.now(UTC)
    if body.outcome == "EXECUTED":
        _move(incident, IncidentStatus.MITIGATED)
    else:
        await _settle(session, incident, a, a.executed_at)
    _stage(session, incident, a, EventType.ACTION_EXECUTED, who.actor,
           f"{a.action} {a.action_args.get('deploy_key', '')}: {body.outcome}"
           + (f" ({body.detail.get('error')})" if body.detail.get("error") else ""),
           source="SYSTEM")  # fmt: skip
    return a, incident


async def list_for_incident(
    session: AsyncSession, ref: UUID | int
) -> tuple[Incident, list[Approval]]:
    incident = await load(session, ref)
    rows = list((await session.scalars(
        select(Approval).where(Approval.incident_id == incident.id)
        .order_by(Approval.requested_at.desc()).limit(50)
    )).all())  # fmt: skip
    return incident, rows


async def list_pending(session: AsyncSession, limit: int = 50) -> list[tuple[Approval, str]]:
    """Phase 17b: the approvals inbox - PENDING across incidents, oldest expiry first (the one
    about to lapse is the one to look at). PENDING exactly as `status_of` computes it, in SQL.
    Served by ix_approvals_undecided_expires_at (0025): expiry is recorded lazily, so undecided
    rows include long-expired ones - the index is ordered by expires_at to skip them."""
    rows = (await session.execute(
        select(Approval, Incident.number).join(Incident, Incident.id == Approval.incident_id)
        .where(Approval.decision.is_(None),  # an outcome needs a decision (DB checks)
               Approval.expires_at > datetime.now(UTC))
        .order_by(Approval.expires_at.asc(), Approval.id).limit(limit)
    )).all()  # fmt: skip
    return [(a, f"INC-{number}") for a, number in rows]  # = Incident.key


async def get(session: AsyncSession, approval_id: UUID) -> tuple[Approval, Incident]:
    a = await session.get(Approval, approval_id)
    if a is None:
        raise NotFoundError("Approval not found.")
    return a, await load(session, a.incident_id)


def summary(a: Approval) -> dict[str, Any]:
    return {"id": str(a.id), "status": status_of(a), "action": a.action}
