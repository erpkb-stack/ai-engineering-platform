"""Client for services/tool-gateway (ADR-017). Agents depend on the `ToolClient` protocol, never
on a DB or HTTP client of an "external" system (AGENTS.md rule 2). Tests use `FakeToolClient`.

Every call carries TWO identities: this service's token (who is calling) and the USER's token
(for whom). The gateway intersects the user's permissions with the agent's allow-list.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from aeoi_models.api.tools import ON_BEHALF_OF_HEADER, ToolResult, ToolSecurity

TokenProvider = Callable[[], Awaitable[str]]
CORRELATION_HEADER = "X-Correlation-ID"


@dataclass(frozen=True)
class CallContext:
    """Per-task context. `user_token` is the human's JWT, forwarded as on-behalf-of."""

    user_token: str
    agent_name: str
    incident_id: uuid.UUID | None = None
    investigation_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    correlation_id: str | None = None


class ToolCallError(Exception):
    """problem+json from the gateway. `reason` is the gateway's stable code
    (missing_permission, not_in_agent_allowlist, timeout, ...). `retryable` = worth retrying
    LATER (the gateway already retried READ tools inside its deadline)."""

    def __init__(
        self, status: int, reason: str, detail: str, tool_call_id: str | None = None
    ) -> None:
        super().__init__(f"{status} {reason}: {detail}")
        self.status = status
        self.reason = reason
        self.detail = detail
        self.tool_call_id = tool_call_id

    @property
    def retryable(self) -> bool:
        return self.status in (429, 503, 504)

    @property
    def denied(self) -> bool:
        return self.status in (401, 403)


class ToolClient(Protocol):
    async def call(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult: ...


class HttpToolClient:
    def __init__(
        self,
        base_url: str,
        service_token: TokenProvider,
        *,
        timeout_s: float = 15.0,  # > the longest tool deadline (10 s) + recording
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, transport=transport, trust_env=False
        )
        self._token = service_token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        body: dict[str, Any] = {"args": args, "agent_name": ctx.agent_name}
        for key in ("incident_id", "investigation_id", "task_id"):
            value = getattr(ctx, key)
            if value is not None:
                body[key] = str(value)
        headers = {
            "Authorization": f"Bearer {await self._token()}",
            ON_BEHALF_OF_HEADER: f"Bearer {ctx.user_token}",
        }
        if ctx.correlation_id:
            headers[CORRELATION_HEADER] = ctx.correlation_id
        try:
            r = await self._http.post(f"/v1/tools/{tool}/invoke", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ToolCallError(
                504, "client_timeout", f"no answer from tool-gateway: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            raise ToolCallError(503, "gateway_unreachable", type(exc).__name__) from exc
        if r.status_code >= 400:
            try:
                p = r.json()
            except json.JSONDecodeError:
                p = {}
            raise ToolCallError(
                r.status_code,
                str(p.get("reason") or "error"),
                str(p.get("detail") or r.text[:300]),
                p.get("tool_call_id"),
            )
        return ToolResult.model_validate(r.json())


@dataclass
class FakeToolClient:
    """Scripted per-tool responses (a dict -> wrapped as a ToolResult; an exception -> raised).
    Records every call so tests can assert the agent used ONLY its allowed tools."""

    script: dict[str, list[dict[str, Any] | Exception]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        self.calls.append({"tool": tool, "args": args, "agent": ctx.agent_name})
        queue = self.script.get(tool)
        if not queue:
            raise ToolCallError(403, "not_in_agent_allowlist", f"fake: no script for {tool}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        call_id = uuid.uuid4()
        kind = item.pop("_kind", "LOG")
        items = item.get("items", [])
        ids = []
        for i, it in enumerate(items):
            it["evidence_id"] = f"{kind}-{call_id.hex}-{i}"
            ids.append(it["evidence_id"])
        flags = item.pop("_flags", [])
        # "_flag_items": [i, ...] -> the real gateway's injection flag for item i
        flags += [
            {"evidence_id": ids[i], "field": "items.message", "patterns": ["fake"]}
            for i in item.pop("_flag_items", [])
            if i < len(ids)
        ]
        return ToolResult(
            tool_call_id=call_id,
            tool=tool,
            data={**item, "truncated": item.get("truncated", False)},
            evidence_ids=ids,
            truncated=bool(item.get("truncated", False)),
            security=ToolSecurity(injection_flags=flags),
            latency_ms=1,
            attempts=1,
        )


__all__ = [
    "CallContext",
    "FakeToolClient",
    "HttpToolClient",
    "ToolCallError",
    "ToolClient",
    "ToolResult",
]
