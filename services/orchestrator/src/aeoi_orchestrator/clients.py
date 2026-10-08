"""HTTP calls the orchestrator makes. Two identities, never mixed:
- AS THE USER (their own token, only inside the start request): read the incident, mark it
  INVESTIGATING. incident-service decides what this user may see/do.
- AS THE ORCHESTRATOR (service token): run an agent (+ the DELEGATED token on behalf of the
  user), post evidence (`evidence:write`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from aeoi_common.correlation import CORRELATION_HEADER, get_correlation_id
from aeoi_models.api.agents import AgentRunResult, EvidenceBatch, EvidenceItem, LogAnalysisTask
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER


class CallError(Exception):
    """A dependency refused or failed. `status` = the HTTP status (0 = no response).
    `retryable` = worth one more try now (unreachable, 502/503); a timeout is NOT retryable:
    the agent may still be working, and a retry would double the work and the deadline."""

    def __init__(self, message: str, status: int = 0, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _cid() -> str:
    return get_correlation_id() or ""


def _detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail", ""))[:300] or r.text[:300]
    except ValueError:
        return r.text[:300]


class IncidentClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, service_token: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._service_token = service_token

    async def get_as_user(self, ref: str, user_token: str) -> dict[str, Any]:
        try:
            r = await self._http.get(
                f"{self._base}/v1/incidents/{ref}",
                headers={"Authorization": f"Bearer {user_token}", CORRELATION_HEADER: _cid()},
            )
        except httpx.TransportError as exc:
            raise CallError(f"incident-service unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise CallError(f"cannot read incident {ref}: {_detail(r)}", r.status_code)
        return r.json()  # type: ignore[no-any-return]

    async def mark_investigating(
        self, incident_id: str, user_token: str, idempotency_key: str
    ) -> dict[str, Any]:
        """incident-service owns the status change, timeline entry and outbox event."""
        try:
            r = await self._http.post(
                f"{self._base}/v1/incidents/{incident_id}/investigate",
                headers={
                    "Authorization": f"Bearer {user_token}",
                    "Idempotency-Key": idempotency_key,
                    CORRELATION_HEADER: _cid(),
                },
            )
        except httpx.TransportError as exc:
            raise CallError(f"incident-service unreachable: {type(exc).__name__}") from exc
        if r.status_code != 202:
            raise CallError(f"cannot investigate: {_detail(r)}", r.status_code)
        return r.json()  # type: ignore[no-any-return]

    async def post_evidence(
        self,
        incident_id: str,
        investigation_id: UUID,
        agent: str,
        items: list[EvidenceItem],
        summary: str,
    ) -> int:
        batch = EvidenceBatch(
            agent=agent, investigation_id=investigation_id, items=items[:200], summary=summary
        )
        last = ""
        for _ in range(2):  # idempotent endpoint (key per evidence id): a retry is safe
            try:
                r = await self._http.post(
                    f"{self._base}/v1/incidents/{incident_id}/evidence",
                    content=batch.model_dump_json(),
                    headers={
                        "Authorization": f"Bearer {self._service_token}",
                        CORRELATION_HEADER: _cid(),
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TransportError as exc:
                last = type(exc).__name__
                continue
            if r.status_code == 200:
                try:
                    return int(r.json()["inserted"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise CallError(f"unexpected evidence response: {r.text[:200]}") from exc
            last = f"HTTP {r.status_code} {_detail(r)}"
            if r.status_code < 500:
                break
        raise CallError(last)


class AgentsClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, service_token: str, timeout_s: float
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout_s

    async def run_log_analysis(self, task: LogAnalysisTask, delegated_token: str) -> AgentRunResult:
        try:
            r = await self._http.post(
                f"{self._base}/v1/agents/log_analysis/run",
                content=task.model_dump_json(),
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    ON_BEHALF_OF_HEADER: f"Bearer {delegated_token}",
                    CORRELATION_HEADER: _cid(),
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise CallError(f"agent timed out after {self._timeout:.0f}s") from exc
        except httpx.TransportError as exc:
            raise CallError(
                f"agents service unreachable: {type(exc).__name__}", retryable=True
            ) from exc
        if r.status_code != 200:
            raise CallError(
                f"agent returned HTTP {r.status_code}: {_detail(r)}",
                r.status_code,
                retryable=r.status_code in (502, 503),
            )
        return AgentRunResult.model_validate(r.json())
