"""Tokens from the OIDC identity provider (Keycloak), verified by EVERY service (Phase 17, ADR-027).

Our directory (identity.users) stays the source of truth: the realm is GENERATED from it
(`scripts/keycloak_realm.py`), and each user carries two attributes the token must hold:
`uid` (identity.users.id) and `aeoi_sub` (identity.users.external_subject). The api's token
exchange (ADR-019) matches BOTH against the directory, exactly as for a dev token.

Rules (each one closes a known attack):
- RS256 only, `kid` required, key from the issuer's JWKS (no `alg=none`, no HS256 confusion);
- exact `iss`, `aud` contains our audience, `exp`/`iat`/`sub` required;
- `typ` = Bearer: an ID token (same signing key) is never an access token;
- `azp` in the allowed clients: a token minted for another client of the realm is refused;
- USER tokens only: no service accounts (`service:` subjects stay with our own issuer), no
  delegated tokens (`act` / `token_use`), never a role we do not know.

Keys: cached per `kid`, fetched only when a token names an unknown `kid` - at most once per
`refetch_interval_s`, with a short timeout - so a slow IdP never stalls every request.
[P] the fetch is a blocking urllib call (rare, bounded); [Prod] prefetch on startup + async.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import jwt

from aeoi_security.auth import (
    ALGORITHM,
    DELEGATED_TOKEN_USE,
    SERVICE_PREFIX,
    AuthError,
    Principal,
)
from aeoi_security.rbac import Role, permissions_for

KNOWN_ROLES = frozenset(r.value for r in Role)
MAX_JWKS_BYTES = 256 * 1024  # a key set is a few KB; never read an unbounded body
Fetch = Callable[[str, float], dict[str, Any]]


def _http_fetch(url: str, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as r:  # noqa: S310 - configured URL
        body = r.read(MAX_JWKS_BYTES + 1)
    if len(body) > MAX_JWKS_BYTES:
        raise ValueError("jwks response too large")
    data: dict[str, Any] = json.loads(body.decode())
    return data


@dataclass(frozen=True)
class OidcConfig:
    issuer: str  # e.g. http://localhost:8090/realms/aeoi - must equal the token's iss exactly
    audience: str = "aeoi-api"
    jwks_url: str = ""  # default: <issuer>/protocol/openid-connect/certs (Keycloak)
    allowed_clients: frozenset[str] = field(default_factory=lambda: frozenset({"aeoi-console"}))
    timeout_s: float = 2.0
    refetch_interval_s: float = 60.0  # at most one fetch per minute for an unknown kid...
    retry_without_keys_s: float = 5.0  # ...but retry soon while NO key is cached (IdP was down)
    key_max_age_s: float = 600.0  # a key the IdP rotated OUT is dropped within 10 minutes
    leeway_s: int = 30

    @property
    def certs_url(self) -> str:
        return self.jwks_url or f"{self.issuer.rstrip('/')}/protocol/openid-connect/certs"


class OidcVerifier:
    def __init__(self, cfg: OidcConfig, fetch: Fetch | None = None) -> None:
        self.cfg = cfg
        self._fetch = fetch or _http_fetch
        self._keys: dict[str, Any] = {}
        self._keys_at = float("-inf")  # when the cached key set was loaded
        self._last_attempt = float("-inf")
        self._lock = threading.Lock()

    def owns(self, token: str) -> bool:
        """Is this token claiming to be from our IdP? (Routing only - nothing is trusted yet.)"""
        try:
            iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError:
            return False
        return bool(iss == self.cfg.issuer)

    def _fresh(self, now: float) -> bool:
        return now - self._keys_at < self.cfg.key_max_age_s

    def _key(self, kid: str) -> Any:
        now = time.monotonic()
        key = self._keys.get(kid)
        if key is not None and self._fresh(now):
            return key
        with self._lock:
            key = self._keys.get(kid)
            wait = self.cfg.refetch_interval_s if self._keys else self.cfg.retry_without_keys_s
            if (key is None or not self._fresh(now)) and now - self._last_attempt >= wait:
                self._last_attempt = now
                try:
                    jwks = jwt.PyJWKSet.from_dict(
                        self._fetch(self.cfg.certs_url, self.cfg.timeout_s)
                    )
                    # REPLACED, not merged: a key the IdP removed is gone (review M3)
                    self._keys = {
                        k.key_id: k.key for k in jwks.keys
                        if k.key_id and k.key_type == "RSA" and k.public_key_use in (None, "sig")
                    }  # fmt: skip
                    self._keys_at = now
                except (OSError, ValueError, jwt.PyJWTError) as exc:
                    if not self._keys:
                        raise AuthError(f"jwks unavailable: {type(exc).__name__}") from exc
                    # IdP unreachable: keep serving the last key set (availability) [P]
                key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown signing key")
        return key

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError(type(exc).__name__) from exc
        if header.get("alg") != ALGORITHM or not header.get("kid"):
            raise AuthError("bad header")
        key = self._key(str(header["kid"]))
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=[ALGORITHM], issuer=self.cfg.issuer,
                audience=self.cfg.audience, leeway=self.cfg.leeway_s,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )  # fmt: skip
        except jwt.PyJWTError as exc:
            raise AuthError(type(exc).__name__) from exc
        if claims.get("token_use") == DELEGATED_TOKEN_USE or "act" in claims:
            raise AuthError("delegated token used as a bearer")
        if claims.get("typ") != "Bearer":
            raise AuthError("not an access token")
        if claims.get("azp") not in self.cfg.allowed_clients:
            raise AuthError("token issued to another client")
        try:
            subject = str(claims["aeoi_sub"])
            if not subject or subject.startswith(SERVICE_PREFIX):
                raise ValueError("bad subject")
            realm = claims.get("realm_access") or {}
            roles = frozenset(str(r) for r in realm.get("roles", [])) & KNOWN_ROLES
            groups = frozenset(str(g).lstrip("/") for g in claims.get("groups", []))
            return Principal(
                subject=subject,
                user_id=UUID(str(claims["uid"])),
                email=str(claims.get("email", "")),
                name=str(claims.get("name", "")),
                roles=roles,
                groups=groups,
                permissions=permissions_for(roles),
                idp=True,
            )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise AuthError("malformed claims (user not provisioned from the directory?)") from exc
