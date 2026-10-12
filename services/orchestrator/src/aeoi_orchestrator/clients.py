"""HTTP calls the orchestrator makes. Two identities, never mixed:
- AS THE USER (their own token, only inside the start request): read the incident, mark it
  INVESTIGATING. incident-service decides what this user may see/do.
- AS THE ORCHESTRATOR (service token): run an agent (+ the DELEGATED token on behalf of the
  user), post evidence (`evidence:write`), hypotheses (`hypotheses:write`), timeline points
  (`timeline:write`) and postmortems (`postmortems:write`).
Phase 16 (ADR-026):
- propose an action FOR the investigation's user (service token `actions:propose` + the
  delegated token): incident-service records that user as the requester;
- decide AS THE APPROVER (their own token, inside their request, never stored);
- record what an execution did (service token `actions:record`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from aeoi_common.correlation import CORRELATION_HEADER, get_correlation_id
from aeoi_models.api.agents import AgentRunResult, AgentTask, EvidenceBatch, EvidenceItem
from aeoi_models.api.approvals import ActionRequest, ActionTask, ApprovalOut, DecisionIn, OutcomeIn
from aeoi_models.api.hypotheses import HypothesisBatch
from aeoi_models.api.postmortem import PostmortemIn, TimelineBatch
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_models.api.release import ReleaseRiskTask
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

    async def post_hypotheses(self, incident_id: str, batch: HypothesisBatch) -> int:
        """Idempotent per (investigation, key): a retry is safe (ADR-021)."""
        last = ""
        for _ in range(2):
            try:
                r = await self._http.post(
                    f"{self._base}/v1/incidents/{incident_id}/hypotheses",
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
                return int(r.json()["inserted"])
            last = f"HTTP {r.status_code} {_detail(r)}"
            if r.status_code < 500:
                break
        raise CallError(last)

    async def _post_idempotent(self, path: str, body: str) -> dict[str, Any]:
        """For endpoints that are idempotent server-side: one retry on transport/5xx."""
        last, status = "", 0
        for _ in range(2):
            try:
                r = await self._http.post(
                    f"{self._base}{path}",
                    content=body,
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
                return r.json()  # type: ignore[no-any-return]
            last, status = f"HTTP {r.status_code} {_detail(r)}", r.status_code
            if r.status_code < 500:
                break
        raise CallError(last, status)

    async def post_timeline(self, incident_id: str, batch: TimelineBatch) -> int:
        """Phase 12: idempotent per (incident, source|subject|time) (ADR-022)."""
        out = await self._post_idempotent(
            f"/v1/incidents/{incident_id}/timeline", batch.model_dump_json()
        )
        return int(out["inserted"])

    async def post_postmortem(self, incident_id: str, pm: PostmortemIn) -> bool:
        """Phase 12: idempotent per (incident, investigation) (ADR-022)."""
        out = await self._post_idempotent(
            f"/v1/incidents/{incident_id}/postmortems", pm.model_dump_json()
        )
        return bool(out["inserted"])

    # --- Phase 16 (ADR-026) ------------------------------------------------------------------

    async def propose_action(
        self, incident_id: str, req: ActionRequest, delegated_token: str
    ) -> ApprovalOut:
        """Idempotent per (investigation, args) at incident-service: one retry is safe."""
        last, status = "", 0
        for _ in range(2):
            try:
                r = await self._http.post(
                    f"{self._base}/v1/incidents/{incident_id}/actions/proposals",
                    content=req.model_dump_json(),
                    headers={
                        "Authorization": f"Bearer {self._service_token}",
                        ON_BEHALF_OF_HEADER: f"Bearer {delegated_token}",
                        CORRELATION_HEADER: _cid(),
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TransportError as exc:
                last = type(exc).__name__
                continue
            if r.status_code == 200:
                return ApprovalOut.model_validate(r.json())
            last, status = f"HTTP {r.status_code} {_detail(r)}", r.status_code
            if r.status_code < 500:
                break
        raise CallError(last, status)

    async def _as_user(
        self, method: str, path: str, user_token: str, body: str | None = None
    ) -> dict[str, Any]:
        """One call AS THE USER. Never retried: a decision is not something to send twice."""
        headers = {"Authorization": f"Bearer {user_token}", CORRELATION_HEADER: _cid()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            r = await self._http.request(method, f"{self._base}{path}", content=body,
                                         headers=headers)  # fmt: skip
        except httpx.TransportError as exc:
            raise CallError(f"incident-service unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise CallError(_detail(r), r.status_code)
        return r.json()  # type: ignore[no-any-return]

    async def get_approval(self, approval_id: UUID, user_token: str) -> ApprovalOut:
        return ApprovalOut.model_validate(
            await self._as_user("GET", f"/v1/approvals/{approval_id}", user_token)
        )

    async def decide(self, approval_id: UUID, d: DecisionIn, user_token: str) -> ApprovalOut:
        """incident-service enforces everything: IC role, not the requester, not decided,
        not expired (an expired one is recorded EXPIRED and answered 409)."""
        return ApprovalOut.model_validate(await self._as_user(
            "POST", f"/v1/approvals/{approval_id}/decision", user_token, d.model_dump_json()
        ))  # fmt: skip

    async def record_outcome(self, approval_id: UUID, o: OutcomeIn) -> ApprovalOut:
        """Idempotent for the same outcome + tool call: one retry is safe."""
        out = await self._post_idempotent(
            f"/v1/approvals/{approval_id}/outcome", o.model_dump_json()
        )
        return ApprovalOut.model_validate(out)


class AgentsClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, service_token: str, timeout_s: float
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout_s

    async def run(
        self,
        agent: str,
        task: AgentTask | ReasoningTask | ReleaseRiskTask | ActionTask,
        delegated_token: str,
        *,
        timeout_s: float | None = None,
    ) -> AgentRunResult:
        """`delegated_token` = whatever on-behalf-of token the task runs as: the delegated one
        for an investigation, the caller's OWN token for release risk and actions."""
        timeout = timeout_s or self._timeout
        try:
            r = await self._http.post(
                f"{self._base}/v1/agents/{agent}/run",
                content=task.model_dump_json(),
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    ON_BEHALF_OF_HEADER: f"Bearer {delegated_token}",
                    CORRELATION_HEADER: _cid(),
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise CallError(f"agent timed out after {timeout:.0f}s") from exc
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
