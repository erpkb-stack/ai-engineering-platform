"""The directory check at the edge (Phase 17a, ADR-027, review H1).

A Keycloak token carries roles from the realm, and the realm is a GENERATED copy of
identity.users. Without this check a user demoted or deactivated in the directory kept their
old roles (approvals, rollbacks, document groups) until someone regenerated the realm.

So for every IdP token at the api - the only public entry point - the directory must agree:
- the user exists with this `uid` AND `aeoi_sub`, and is active (else 401: sign in again);
- the token's roles are a SUBSET of the directory's roles (else 403: the realm is out of date).
  A user promoted in the directory but not yet in the realm simply has fewer rights (least
  privilege), never more.
Cached per user for `ttl_s` (60 s): a demotion takes effect within a minute at the edge.
Fail closed: no directory = no IdP users. Static-issuer tokens are untouched (they are minted
FROM the directory by `make token`).

Behind the api, services trust the token itself, bounded by its 5-minute lifetime and by the
network: in production only the api is reachable from outside ([Prod] network policy).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import ForbiddenError, UnauthorizedError, UpstreamUnavailableError
from aeoi_db.models.identity import User, UserRole
from aeoi_security.auth import Principal


@dataclass(frozen=True)
class _Entry:
    subject: str
    active: bool
    roles: frozenset[str]


class DirectoryCheck:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession] | None, ttl_s: float = 60.0
    ) -> None:
        self._sessions = sessions
        self._ttl = ttl_s
        self._cache: dict[UUID, tuple[float, _Entry | None]] = {}

    async def _load(self, uid: UUID) -> _Entry | None:
        hit = self._cache.get(uid)
        now = time.monotonic()
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        if self._sessions is None:
            raise UpstreamUnavailableError(
                "The user directory is not configured: IdP sign-in refused."
            )
        async with self._sessions() as s:
            user = await s.get(User, uid)
            entry = None
            if user is not None:
                roles = (await s.scalars(select(UserRole.role_name)
                                         .where(UserRole.user_id == uid))).all()  # fmt: skip
                entry = _Entry(user.external_subject, bool(user.is_active), frozenset(roles))
        if len(self._cache) > 10_000:  # bounded: a flood of distinct uids cannot grow it forever
            self._cache.clear()
        self._cache[uid] = (now, entry)
        return entry

    async def check(self, principal: Principal) -> None:
        if not principal.idp:
            return
        entry = await self._load(principal.user_id)
        if entry is None or entry.subject != principal.subject or not entry.active:
            raise UnauthorizedError("Not an active user in the directory.")
        extra = principal.roles - entry.roles
        if extra:
            raise ForbiddenError(
                f"Your sign-in grants roles the directory does not ({', '.join(sorted(extra))}): "
                "sign in again; if it persists the identity provider is out of date."
            )
