"""Audit API.

Write: POST /v1/events - SERVICE tokens with scope `audit:write` only. Idempotent on event id
       (ON CONFLICT DO NOTHING), so producers can redeliver freely. The receiver stamps WHO
       delivered each event (`details._ingested_by`): a producer can't hide which service it is.
Read:  GET /v1/events - USER tokens. The scope is decided HERE from permissions, never from a
       parameter:  audit:read_all -> everything | audit:read_own -> events by me or on my behalf.
       audit:read_incident means "the incidents I command"; nothing records that assignment
       yet (Phase 16), so it is treated as read_own - an `incident_id` parameter only NARROWS.
       audit:read_team -> 403 until team membership exists (Phase 26). Refusing is honest;
       guessing a team or trusting a parameter is a data leak.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import ForbiddenError, ValidationFailedError
from aeoi_models.api.audit import AuditBatch, AuditEventOut, AuditIngestResult, AuditPage
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web import CurrentPrincipal, require_scope

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["audit"])
Writer = Annotated[Principal, Depends(require_scope("audit:write"))]

_INSERT = text("""
INSERT INTO audit.audit_events (id, occurred_at, actor, actor_role, action, resource_type,
                                resource_id, incident_id, correlation_id, outcome, details)
SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(
    id uuid, occurred_at timestamptz, actor text, actor_role text, action text,
    resource_type text, resource_id text, incident_id uuid, correlation_id text,
    outcome text, details jsonb)
ON CONFLICT (id, occurred_at) DO NOTHING
RETURNING id
""")


@router.post("/events", response_model=AuditIngestResult)
async def ingest(batch: AuditBatch, principal: Writer, request: Request) -> AuditIngestResult:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    rows = []
    for e in batch.events:
        row = e.model_dump(mode="json")
        row["details"] = {**e.details, "_ingested_by": principal.subject}
        rows.append(row)
    async with sessions() as s, s.begin():
        inserted = len((await s.execute(_INSERT, {"rows": json.dumps(rows)})).all())
    log.info("audit_ingest", source=principal.subject, received=len(rows), inserted=inserted)
    return AuditIngestResult(received=len(rows), inserted=inserted)


def _encode(ts: datetime, id_: UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{id_}".encode()).decode()


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, id_ = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), UUID(id_)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailedError("Invalid cursor.") from exc


@router.get("/events", response_model=AuditPage)
async def query(
    request: Request,
    principal: CurrentPrincipal,
    since: datetime | None = None,
    until: datetime | None = None,
    incident_id: UUID | None = None,
    actor: Annotated[str | None, Query(max_length=160)] = None,
    action: Annotated[str | None, Query(max_length=80)] = None,
    outcome: Annotated[str | None, Query(pattern="^(SUCCESS|DENIED|FAILURE)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> AuditPage:
    if principal.is_service:
        raise ForbiddenError("Audit reads are for users (service tokens write only).")
    now = datetime.now(UTC)
    until = until or now
    since = since or until - timedelta(days=7)
    if since.tzinfo is None or until.tzinfo is None:
        raise ValidationFailedError("since/until must include a timezone.")
    max_days = request.app.state.settings.max_query_window_days
    if until - since > timedelta(days=max_days) or until <= since:
        raise ValidationFailedError(f"Window must be positive and at most {max_days} days.")

    where = ["occurred_at >= :since", "occurred_at < :until"]
    params: dict[str, Any] = {"since": since, "until": until, "limit": limit + 1}
    if principal.has(Perm.AUDIT_READ_ALL):
        scope = "all"
    elif principal.has(Perm.AUDIT_READ_OWN) or principal.has(Perm.AUDIT_READ_INCIDENT):
        scope = "own"
        # [P] the OR on details is a scan inside the (<= 2) partitions of the window.
        # [Prod] a subject_user_id column + index, filled by the producer.
        where.append("(actor = :me OR details->>'on_behalf_of' = :me_id)")
        params |= {"me": f"user:{principal.user_id}", "me_id": str(principal.user_id)}
    elif principal.has(Perm.AUDIT_READ_TEAM):
        raise ForbiddenError("Team audit scope needs team membership data (not before Phase 26).")
    else:
        raise ForbiddenError("Missing audit read permission.")

    for col, val in (("incident_id", incident_id), ("actor", actor), ("action", action),
                     ("outcome", outcome)):  # fmt: skip
        if val is not None:
            where.append(f"{col} = :{col}")
            params[col] = val
    if cursor:
        c_ts, c_id = _decode(cursor)
        where.append("(occurred_at, id) < (:c_ts, :c_id)")
        params |= {"c_ts": c_ts, "c_id": c_id}
    sql = text(
        "SELECT id, occurred_at, actor, actor_role, action, resource_type, resource_id, "  # noqa: S608 - fixed column names only
        "incident_id, correlation_id, outcome, details FROM audit.audit_events "
        f"WHERE {' AND '.join(where)} ORDER BY occurred_at DESC, id DESC LIMIT :limit"
    )
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessions() as s:
        rows = (await s.execute(sql, params)).mappings().all()
    items = [AuditEventOut(**r) for r in rows[:limit]]
    nxt = _encode(items[-1].occurred_at, items[-1].id) if len(rows) > limit and items else None
    return AuditPage(items=items, next_cursor=nxt, scope=scope)  # type: ignore[arg-type]
