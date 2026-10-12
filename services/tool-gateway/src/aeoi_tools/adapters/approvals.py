"""Approval verification at the system of record (Phase 16, ADR-026).

Bearer = the gateway's own service token (scope `approvals:consume`); X-On-Behalf-Of = the
ACTING user's own token (never a delegated one - incident-service refuses those here). The
gateway never decides whether an approval is valid: incident-service does, and marks it used
in the same transaction. Any failure is a refusal - fail closed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import httpx

from aeoi_common.correlation import CORRELATION_HEADER
from aeoi_models.api.approvals import ConsumeIn, ConsumeOut
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER


@dataclass
class ApprovalRefusedError(Exception):
    status: int  # 403 / 409 from incident-service, 503 when it cannot be asked
    detail: str


class ApprovalsClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        service_token: Callable[[], Awaitable[str]],
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._service_token = service_token

    async def consume(
        self, approval_id: UUID, body: ConsumeIn, user_token: str, correlation_id: str | None
    ) -> ConsumeOut:
        try:
            own = await self._service_token()
        except OSError as exc:
            raise ApprovalRefusedError(
                503, "approvals cannot be verified: run make tools-tokens"
            ) from exc
        headers = {"Authorization": f"Bearer {own}", ON_BEHALF_OF_HEADER: f"Bearer {user_token}"}
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            r = await self._client.post(
                f"{self._base}/v1/approvals/{approval_id}/consume",
                json=body.model_dump(mode="json"), headers=headers,
            )  # fmt: skip
        except httpx.HTTPError as exc:
            raise ApprovalRefusedError(
                503, f"incident-service unreachable: {type(exc).__name__}"
            ) from exc
        if r.status_code == 200:
            return ConsumeOut.model_validate(r.json())
        try:
            detail = str(r.json().get("detail") or r.json().get("title") or "")
        except ValueError:
            detail = ""
        status = r.status_code if r.status_code in (403, 404, 409) else 503
        raise ApprovalRefusedError(
            status, (detail or f"incident-service returned {r.status_code}")[:300]
        )
