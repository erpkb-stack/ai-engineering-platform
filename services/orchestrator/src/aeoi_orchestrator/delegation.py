"""Client for the api's token exchange (ADR-019). The orchestrator never stores a user token:
it trades it once for a grant, then mints short-lived delegated tokens from the grant.

Tokens live in memory only (a restart refreshes from the grant). A refused refresh is
final for the run: the grant was revoked, expired, or the user lost access.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import structlog

from aeoi_common.correlation import CORRELATION_HEADER, get_correlation_id
from aeoi_models.api.delegation import TOKEN_TYPE_ACCESS, DelegatedToken, DelegationRevoke

log = structlog.get_logger(__name__)


class DelegationError(Exception):
    """The exchange refused or failed. `final` = retrying cannot help (revoked/denied)."""

    def __init__(self, message: str, *, final: bool) -> None:
        super().__init__(message)
        self.final = final


@dataclass
class _Cached:
    token: str
    expires_at: datetime


class DelegationClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_url: str,
        service_token: str,
        *,
        refresh_margin: timedelta = timedelta(seconds=60),
        timeout_s: float = 10.0,
    ) -> None:
        self._http = http
        self._base = api_url.rstrip("/") + "/internal/v1/delegations"
        self._service_token = service_token
        self._margin = refresh_margin
        self._timeout = timeout_s
        self._cache: dict[UUID, _Cached] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._service_token}",
            CORRELATION_HEADER: get_correlation_id() or "",
            "Content-Type": "application/json",
        }

    async def _post(self, url: str, body: str) -> httpx.Response:
        try:
            return await self._http.post(
                url, content=body, headers=self._headers(), timeout=self._timeout
            )
        except httpx.TransportError as exc:
            raise DelegationError(
                f"token exchange unreachable: {type(exc).__name__}", final=False
            ) from exc

    @staticmethod
    def _refused(r: httpx.Response) -> DelegationError:
        try:
            detail = str(r.json().get("detail", ""))[:300]
        except ValueError:
            detail = r.text[:300]
        if r.status_code == 401:  # OUR token, not the user's access (review finding)
            return DelegationError(
                "the orchestrator's own service token was rejected (expired? run "
                "`make agents-tokens` and restart run-orch)",
                final=True,
            )
        # 4xx = a decision (denied, revoked, unknown); 5xx = the exchange is unwell
        return DelegationError(f"HTTP {r.status_code}: {detail}", final=r.status_code < 500)

    def _remember(self, d: DelegatedToken) -> str:
        token = d.access_token.get_secret_value()
        self._cache[d.grant_id] = _Cached(token, d.expires_at)
        return token

    async def create(
        self, user_token: str, investigation_id: UUID, incident_id: UUID
    ) -> DelegatedToken:
        # explicit body: a pydantic dump would mask the SecretStr (that is its job)
        payload = json.dumps(
            {
                "subject_token": user_token,
                "subject_token_type": TOKEN_TYPE_ACCESS,
                "investigation_id": str(investigation_id),
                "incident_id": str(incident_id),
            }
        )
        r = await self._post(self._base, payload)
        if r.status_code != 201:
            raise self._refused(r)
        d = DelegatedToken.model_validate(r.json())
        self._remember(d)
        return d

    async def token(self, grant_id: UUID) -> str:
        """A delegated token with at least `refresh_margin` left (refreshing if needed)."""
        lock = self._locks.setdefault(grant_id, asyncio.Lock())
        async with lock:  # parallel branches of one investigation refresh once, not N times
            cached = self._cache.get(grant_id)
            if cached and cached.expires_at - datetime.now(UTC) > self._margin:
                return cached.token
            r = await self._post(f"{self._base}/{grant_id}/token", "{}")
            if r.status_code != 200:
                self._cache.pop(grant_id, None)
                raise self._refused(r)
            return self._remember(DelegatedToken.model_validate(r.json()))

    async def revoke(self, grant_id: UUID, reason: str) -> None:
        """Best effort: a failed revoke leaves the grant to expire on its own (logged)."""
        self._cache.pop(grant_id, None)
        self._locks.pop(grant_id, None)
        try:
            r = await self._post(
                f"{self._base}/{grant_id}/revoke",
                DelegationRevoke(reason=reason[:200]).model_dump_json(),
            )
        except DelegationError as exc:
            log.warning("delegation_revoke_failed", grant_id=str(grant_id), error=str(exc))
            return
        if r.status_code != 200:
            log.warning("delegation_revoke_failed", grant_id=str(grant_id), status=r.status_code)
