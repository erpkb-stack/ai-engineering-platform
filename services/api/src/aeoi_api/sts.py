"""Token exchange / delegation service (ADR-019). The api owns `identity`, so it is the STS.

[P]    here, signing with secrets/delegation_private.pem.
[Prod] the corporate IdP's RFC 8693 token-exchange endpoint; this module disappears.

Rules (each has a test in tests/integration/security/test_delegation.py):
- Caller: a SERVICE token with scope `delegation:create`. Users can never call this.
- Create: the subject token must be a valid USER token (not a service, not delegated). The
  user is re-loaded from identity.users (active, same sub) and roles/groups come from the DB,
  not from the token. `investigations:run` is required. One grant per investigation.
- Refresh: only the grant's actor; not revoked, not expired; the user is re-checked every
  time (inactive or lost `investigations:run` -> the grant is revoked with that reason).
- Every create / issue / deny / revoke is an identity.delegation_events row. Denials are
  committed in their own transaction BEFORE the error is raised, so they survive the 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamUnavailableError,
)
from aeoi_common.ids import uuid7
from aeoi_db.models.identity import DelegationEvent, DelegationGrant, User, UserGroup, UserRole
from aeoi_models.api.delegation import (
    DELEGATION_SCOPE,
    DelegatedToken,
    DelegationCreate,
    DelegationRevoke,
    DelegationState,
)
from aeoi_security.auth import AuthError, Principal, issue_delegated_token
from aeoi_security.rbac import Perm, permissions_for
from aeoi_web import require_scope

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/internal/v1/delegations", tags=["delegation"])
Caller = Annotated[Principal, Depends(require_scope(DELEGATION_SCOPE))]


@dataclass(frozen=True)
class StsConfig:
    private_key: str
    grant_ttl: timedelta
    token_ttl: timedelta


@dataclass(frozen=True)
class _UserRow:
    id: UUID
    subject: str
    email: str
    name: str
    roles: list[str]
    groups: list[str]


def _deps(request: Request) -> tuple[async_sessionmaker[AsyncSession], StsConfig]:
    sessions = getattr(request.app.state, "sts_sessions", None)
    config = getattr(request.app.state, "sts_config", None)
    if sessions is None or config is None:
        raise UpstreamUnavailableError(
            "Token exchange is not configured (run `make delegation-keys` and `make db-users`)."
        )
    return sessions, config


async def _load_user(s: AsyncSession, user_id: UUID, subject: str) -> _UserRow | None:
    """The user as the DIRECTORY knows them now (not as their token claims)."""
    user = await s.scalar(
        select(User).where(User.id == user_id, User.external_subject == subject, User.is_active)
    )
    if user is None:
        return None
    roles = (await s.scalars(select(UserRole.role_name).where(UserRole.user_id == user.id))).all()
    groups = (
        await s.scalars(select(UserGroup.group_name).where(UserGroup.user_id == user.id))
    ).all()
    return _UserRow(
        user.id, user.external_subject, user.email, user.display_name, sorted(roles), sorted(groups)
    )


async def _deny(
    sessions: async_sessionmaker[AsyncSession],
    *,
    actor: str,
    detail: str,
    grant_id: UUID | None = None,
    investigation_id: UUID | None = None,
    user_id: UUID | None = None,
) -> None:
    async with sessions() as s, s.begin():
        s.add(
            DelegationEvent(
                id=uuid7(),
                grant_id=grant_id,
                investigation_id=investigation_id,
                user_id=user_id,
                actor=actor[:120],
                event="DENIED",
                detail=detail[:500],
            )
        )
    log.warning(
        "delegation_denied",
        actor=actor,
        detail=detail,
        grant_id=str(grant_id) if grant_id else None,
        investigation_id=str(investigation_id) if investigation_id else None,
    )


def _mint(
    grant: DelegationGrant, user: _UserRow, config: StsConfig, now: datetime
) -> DelegatedToken:
    expires = min(now + config.token_ttl, grant.expires_at)
    token = issue_delegated_token(
        private_key=config.private_key,
        subject=user.subject,
        user_id=user.id,
        email=user.email,
        name=user.name,
        roles=user.roles,
        groups=user.groups,
        actor=grant.actor,
        investigation_id=grant.investigation_id,
        incident_id=grant.incident_id,
        grant_id=grant.id,
        expires_at=expires,
        now=now,
    )
    return DelegatedToken(
        access_token=SecretStr(token),
        expires_in=max(1, int((expires - now).total_seconds())),
        expires_at=expires,
        grant_id=grant.id,
        grant_expires_at=grant.expires_at,
        investigation_id=grant.investigation_id,
        incident_id=grant.incident_id,
    )


def _issued(s: AsyncSession, grant: DelegationGrant, now: datetime, detail: str) -> None:
    grant.tokens_issued = (grant.tokens_issued or 0) + 1
    grant.last_issued_at = now
    s.add(
        DelegationEvent(
            id=uuid7(),
            grant_id=grant.id,
            investigation_id=grant.investigation_id,
            user_id=grant.user_id,
            actor=grant.actor,
            event="ISSUED",
            detail=detail,
        )
    )


def _live_problem(grant: DelegationGrant, now: datetime) -> str | None:
    if grant.revoked_at is not None:
        return f"grant revoked: {grant.revoked_reason}"
    if grant.expires_at <= now:
        return "grant expired"
    return None


@router.post("", status_code=201, response_model=DelegatedToken)
async def create_delegation(body: DelegationCreate, request: Request, caller: Caller) -> Any:
    try:
        return await _create(body, request, caller)
    except IntegrityError as exc:  # two creates for one investigation raced: one wins
        if "uq_delegation_grants_investigation_id" not in str(exc.orig):
            raise
        raise ConflictError("A grant for this investigation was created concurrently.") from exc


async def _create(body: DelegationCreate, request: Request, caller: Caller) -> Any:
    sessions, config = _deps(request)
    try:
        user = request.app.state.auth.verify(body.subject_token.get_secret_value())
    except AuthError:
        await _deny(
            sessions,
            actor=caller.subject,
            detail="subject token invalid or expired",
            investigation_id=body.investigation_id,
        )
        raise ForbiddenError("Subject token is invalid or expired.") from None
    if user.is_service:
        await _deny(
            sessions,
            actor=caller.subject,
            detail=f"subject is a service ({user.subject})",
            investigation_id=body.investigation_id,
        )
        raise ForbiddenError("Only a USER can be delegated.")
    now = datetime.now(UTC)
    deny: str | None = None
    async with sessions() as s, s.begin():
        existing = await s.scalar(
            select(DelegationGrant)
            .where(DelegationGrant.investigation_id == body.investigation_id)
            .with_for_update()
        )
        conflict: str | None = None
        if existing is not None:
            if (
                existing.user_id != user.user_id
                or existing.actor != caller.subject
                or existing.incident_id != body.incident_id
            ):
                conflict = "this investigation already has a grant for another party"
            elif problem := _live_problem(existing, now):
                conflict = f"this investigation's grant is not usable: {problem}"
            else:
                row = await _load_user(s, user.user_id, user.subject)
                if row is None or Perm.INVESTIGATIONS_RUN not in permissions_for(row.roles):
                    # same rule as refresh: lost access revokes the grant (review finding)
                    await _revoke(s, existing, "system:sts", "user lost access before re-create")
                    deny = "user inactive or lacks investigations:run (grant revoked)"
                else:
                    _issued(s, existing, now, "create retried (idempotent)")
                    return _mint(existing, row, config, now)
        else:
            row = await _load_user(s, user.user_id, user.subject)
            if row is None:
                deny = "user unknown or inactive in the directory"
            elif Perm.INVESTIGATIONS_RUN not in permissions_for(row.roles):
                deny = "user lacks investigations:run (directory roles)"
            else:
                grant = DelegationGrant(
                    id=uuid7(),
                    user_id=row.id,
                    actor=caller.subject,
                    incident_id=body.incident_id,
                    investigation_id=body.investigation_id,
                    created_at=now,
                    expires_at=now + config.grant_ttl,
                )
                s.add(grant)
                await s.flush()
                s.add(
                    DelegationEvent(
                        id=uuid7(),
                        grant_id=grant.id,
                        investigation_id=grant.investigation_id,
                        user_id=row.id,
                        actor=caller.subject,
                        event="CREATED",
                        detail=f"roles={','.join(row.roles)}",
                    )
                )
                _issued(s, grant, now, "initial token")
                log.info(
                    "delegation_created",
                    grant_id=str(grant.id),
                    investigation_id=str(grant.investigation_id),
                    actor=caller.subject,
                    user=str(row.id),
                )
                return _mint(grant, row, config, now)
    await _deny(
        sessions,
        actor=caller.subject,
        detail=conflict or deny or "denied",
        investigation_id=body.investigation_id,
        user_id=user.user_id,
    )
    if conflict:
        raise ConflictError(f"Delegation refused: {conflict}.")
    raise ForbiddenError(f"Delegation refused: {deny}.")


async def _revoke(s: AsyncSession, grant: DelegationGrant, actor: str, reason: str) -> None:
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_reason = reason[:200]
    s.add(
        DelegationEvent(
            id=uuid7(),
            grant_id=grant.id,
            investigation_id=grant.investigation_id,
            user_id=grant.user_id,
            actor=actor[:120],
            event="REVOKED",
            detail=reason[:500],
        )
    )


@router.post("/{grant_id}/token", response_model=DelegatedToken)
async def refresh_delegation(grant_id: UUID, request: Request, caller: Caller) -> Any:
    sessions, config = _deps(request)
    now = datetime.now(UTC)
    deny: str | None = None
    unknown = False
    async with sessions() as s, s.begin():
        grant = await s.scalar(
            select(DelegationGrant).where(DelegationGrant.id == grant_id).with_for_update()
        )
        if grant is None:
            unknown = True
        elif grant.actor != caller.subject:
            deny = f"caller {caller.subject} is not the grant's actor"
        else:
            deny = _live_problem(grant, now)
        if grant is not None and deny is None:
            row = await _load_user(s, grant.user_id, await _subject_of(s, grant.user_id))
            if row is None:
                await _revoke(s, grant, "system:sts", "user deactivated")
                deny = "user deactivated (grant revoked)"
            elif Perm.INVESTIGATIONS_RUN not in permissions_for(row.roles):
                await _revoke(s, grant, "system:sts", "user lost investigations:run")
                deny = "user lost investigations:run (grant revoked)"
            else:
                _issued(s, grant, now, "refresh")
                return _mint(grant, row, config, now)
    if unknown or grant is None:
        await _deny(sessions, actor=caller.subject, detail="refresh of an unknown grant",
                    grant_id=grant_id)  # fmt: skip
        raise NotFoundError("Unknown grant.")
    await _deny(
        sessions,
        actor=caller.subject,
        detail=deny or "denied",
        grant_id=grant_id,
        investigation_id=grant.investigation_id,
        user_id=grant.user_id,
    )
    raise ForbiddenError(f"Delegation refused: {deny}.")


async def _subject_of(s: AsyncSession, user_id: UUID) -> str:
    return str(await s.scalar(select(User.external_subject).where(User.id == user_id)) or "")


@router.post("/{grant_id}/revoke", response_model=DelegationState)
async def revoke_delegation(
    grant_id: UUID, body: DelegationRevoke, request: Request, caller: Caller
) -> Any:
    sessions, _ = _deps(request)
    refused: str | None = None
    async with sessions() as s, s.begin():
        grant = await s.scalar(
            select(DelegationGrant).where(DelegationGrant.id == grant_id).with_for_update()
        )
        if grant is None:
            refused = "revoke of an unknown grant"
        elif grant.actor != caller.subject:
            refused = f"caller {caller.subject} is not the grant's actor (revoke)"
        elif grant.revoked_at is None:  # idempotent: a second revoke keeps the first reason
            await _revoke(s, grant, caller.subject, body.reason)
        if refused is None and grant is not None:
            return DelegationState(
                grant_id=grant.id,
                investigation_id=grant.investigation_id,
                revoked=True,
                revoked_reason=grant.revoked_reason,
                expires_at=grant.expires_at,
            )
    await _deny(sessions, actor=caller.subject, detail=refused or "denied", grant_id=grant_id)
    if grant is None:
        raise NotFoundError("Unknown grant.")
    raise ForbiddenError("Only the grant's actor may revoke it.")


async def live_grants(sessions: async_sessionmaker[AsyncSession]) -> int:
    """Readiness/metrics aid: grants that can still mint tokens."""
    async with sessions() as s:
        n = await s.scalar(
            select(func.count())
            .select_from(DelegationGrant)
            .where(DelegationGrant.revoked_at.is_(None), DelegationGrant.expires_at > func.now())
        )
    return int(n or 0)
