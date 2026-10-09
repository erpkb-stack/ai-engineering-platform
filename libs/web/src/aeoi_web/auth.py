"""FastAPI dependencies for authentication (401) and permission checks (403)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from aeoi_common.errors import ForbiddenError, UnauthorizedError
from aeoi_security.auth import AuthError, Principal, verify_delegated_token, verify_token
from aeoi_security.rbac import Perm

_bearer = HTTPBearer(auto_error=False)
# same value as aeoi_models.api.tools.ON_BEHALF_OF_HEADER (this lib must not import models)
ON_BEHALF_OF_HEADER = "X-On-Behalf-Of"


class Authenticator:
    """Holds the verification key/issuer/audience; stored on app.state.auth."""

    def __init__(
        self,
        public_key: str,
        issuer: str,
        audience: str,
        *,
        delegation_public_key: str | None = None,
    ) -> None:
        self.public_key = public_key
        self.issuer = issuer
        self.audience = audience
        # None = this service does not accept delegated tokens at all (ADR-019)
        self.delegation_public_key = delegation_public_key

    def verify(self, token: str) -> Principal:
        """PRIMARY bearer: user or service tokens from the IdP. Never a delegated token."""
        return verify_token(
            token, public_key=self.public_key, issuer=self.issuer, audience=self.audience
        )

    def verify_obo(self, token: str) -> Principal:
        """The on-behalf-of slot (next to an authenticated SERVICE token): a USER principal
        from a user token or, if configured, a delegated token. Services are refused."""
        try:
            user = self.verify(token)
        except AuthError:
            if self.delegation_public_key is None:
                raise
            user = verify_delegated_token(token, public_key=self.delegation_public_key)
        if user.is_service:
            raise AuthError("on-behalf-of must be a user")
        return user


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


def require_acting_user(
    perm: Perm, obo_scope: str, *, allow_delegated: bool = True
) -> Callable[..., Awaitable[Principal]]:
    """Dependency: the USER this request acts for, holding `perm` (ADR-020).

    - a user token as the bearer: that user (unchanged behaviour);
    - a SERVICE token with `obo_scope` + `X-On-Behalf-Of: Bearer <user or delegated token>`:
      the on-behalf-of user (`Authenticator.verify_obo`, so a delegated token is accepted
      here and only here);
    - a service token without the scope, or without the header: 403. A service never acts as
      itself on user data (no confused deputy). A delegated token as the bearer: 401.
    `allow_delegated=False`: only a USER token in the on-behalf-of slot (an endpoint the
    investigation flow never needs - least privilege for the delegated token, review finding).
    """

    async def _check(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        user = principal
        if principal.is_service:
            if obo_scope not in principal.scopes:
                raise ForbiddenError(f"Service calls need scope '{obo_scope}'.")
            raw = request.headers.get(ON_BEHALF_OF_HEADER, "")
            scheme, _, token = raw.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise ForbiddenError(f"{ON_BEHALF_OF_HEADER}: Bearer <user token> is required.")
            auth: Authenticator = request.app.state.auth
            try:
                user = auth.verify_obo(token.strip())
            except AuthError as exc:
                raise ForbiddenError("On-behalf-of token is invalid.") from exc
            if user.delegation is not None and not allow_delegated:
                raise ForbiddenError("Delegated tokens are not accepted on this endpoint.")
            request.state.via = principal.actor
        if not user.has(perm):
            raise ForbiddenError(f"Missing permission '{perm}'.")
        request.state.principal = user
        return user

    return _check


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
