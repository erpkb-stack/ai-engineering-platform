"""JWT verification and the authenticated Principal.

[P]  RS256 tokens from the local dev issuer (`make token`), verified with a PEM public key.
[Prod] tokens from the corporate OIDC IdP, verified with its JWKS (same code path: only
       the key source changes - see `jwks_url` in the API settings, Phase 26).

Hard rules:
- Algorithm is pinned (RS256). `alg=none` and HS256-with-public-key confusion are rejected.
- `exp`, `iat`, `iss`, `aud`, `sub` are required.
- Identity comes ONLY from the verified token, never from a request body or header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt

from aeoi_security.rbac import Perm, permissions_for

ALGORITHM = "RS256"
DEFAULT_ISSUER = "aeoi-dev-issuer"
DEFAULT_AUDIENCE = "aeoi-api"


class AuthError(Exception):
    """Token missing/invalid. Maps to HTTP 401 (never leak *why* to the client)."""


@dataclass(frozen=True)
class Principal:
    subject: str  # OIDC "sub"
    user_id: UUID
    email: str
    name: str
    roles: frozenset[str]
    groups: frozenset[str]
    permissions: frozenset[Perm] = field(default_factory=frozenset)

    @property
    def actor(self) -> str:
        """Actor string used in events/audit: user:<uuid>."""
        return f"user:{self.user_id}"

    def has(self, perm: Perm) -> bool:
        return perm in self.permissions


def verify_token(
    token: str,
    *,
    public_key: str,
    issuer: str = DEFAULT_ISSUER,
    audience: str = DEFAULT_AUDIENCE,
    leeway_s: int = 30,
) -> Principal:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
            issuer=issuer,
            audience=audience,
            leeway=leeway_s,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(type(exc).__name__) from exc
    try:
        roles = frozenset(str(r) for r in claims.get("roles", []))
        return Principal(
            subject=str(claims["sub"]),
            user_id=UUID(str(claims["uid"])),
            email=str(claims.get("email", "")),
            name=str(claims.get("name", "")),
            roles=roles,
            groups=frozenset(str(g) for g in claims.get("groups", [])),
            permissions=permissions_for(roles),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AuthError("malformed claims") from exc


def issue_token(
    *,
    private_key: str,
    subject: str,
    user_id: UUID,
    email: str,
    name: str,
    roles: list[str],
    groups: list[str],
    ttl: timedelta = timedelta(hours=1),
    issuer: str = DEFAULT_ISSUER,
    audience: str = DEFAULT_AUDIENCE,
    now: datetime | None = None,
) -> str:
    """DEV/TEST ONLY. Production tokens come from the IdP, never from this function."""
    issued = now or datetime.now(UTC)
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "uid": str(user_id),
        "email": email,
        "name": name,
        "roles": roles,
        "groups": groups,
        "iat": int(issued.timestamp()),
        "exp": int((issued + ttl).timestamp()),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, private_key, algorithm=ALGORITHM)
