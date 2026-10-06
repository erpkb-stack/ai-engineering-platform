"""FastAPI dependencies for authentication (401) and permission checks (403)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from aeoi_common.errors import ForbiddenError, UnauthorizedError
from aeoi_security.auth import AuthError, Principal, verify_token
from aeoi_security.rbac import Perm

_bearer = HTTPBearer(auto_error=False)


class Authenticator:
    """Holds the verification key/issuer/audience; stored on app.state.auth."""

    def __init__(self, public_key: str, issuer: str, audience: str) -> None:
        self.public_key = public_key
        self.issuer = issuer
        self.audience = audience

    def verify(self, token: str) -> Principal:
        return verify_token(
            token, public_key=self.public_key, issuer=self.issuer, audience=self.audience
        )


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing bearer token.")
    auth: Authenticator = request.app.state.auth
    try:
        principal = auth.verify(credentials.credentials)
    except AuthError as exc:
        raise UnauthorizedError("Invalid or expired token.") from exc
    request.state.principal = principal
    return principal


def require(perm: Perm) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency: authenticated AND holds `perm`. Usage: Depends(require(Perm.INCIDENTS_READ))."""

    async def _check(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.has(perm):
            raise ForbiddenError(f"Missing permission '{perm}'.")
        return principal

    return _check


def require_scope(scope: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency for service-to-service endpoints: a SERVICE principal holding `scope`.

    User tokens are rejected even if they somehow carry a scope claim: an end user must never
    call internal machinery (e.g. the LLM gateway) directly.
    """

    async def _check(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.is_service or scope not in principal.scopes:
            raise ForbiddenError(f"Requires a service token with scope '{scope}'.")
        return principal

    return _check


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
