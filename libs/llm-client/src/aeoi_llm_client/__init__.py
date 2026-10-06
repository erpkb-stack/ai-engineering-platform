"""Client for services/llm-gateway. Agents depend on the `LLMClient` protocol, never on a
vendor SDK (AGENTS.md rule 6). Tests use `FakeLLMClient` - no network, deterministic."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

TokenProvider = Callable[[], Awaitable[str]]


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CallMetadata(BaseModel):
    prompt_id: str | None = None
    prompt_version: int | None = None
    agent_name: str | None = None
    investigation_id: uuid.UUID | None = None


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    estimated: bool = False


class LLMResponse(BaseModel):
    request_id: str
    route: str
    model: str
    provider: str
    text: str
    data: dict[str, Any] | None = None
    usage: LLMUsage
    cost_usd: Decimal
    latency_ms: int
    fallback_used: bool
    cached: bool
    repaired: bool = False
    redactions: int = 0
    usage_recorded: bool = True
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class EmbedResponse(BaseModel):
    request_id: str
    model: str
    dimensions: int
    vectors: list[list[float]]
    usage: LLMUsage
    cost_usd: Decimal
    latency_ms: int


class LLMError(Exception):
    """problem+json from the gateway. Branch on `type` (e.g. ...llm-budget-exceeded)."""

    def __init__(self, status: int, type_: str, detail: str) -> None:
        super().__init__(f"{status} {type_}: {detail}")
        self.status = status
        self.type = type_
        self.detail = detail


class LLMClient(Protocol):
    async def generate(
        self,
        messages: list[Msg],
        *,
        route: str = ...,
        system: str | None = ...,
        max_tokens: int = ...,
        temperature: float = ...,
        allow_fallback: bool = ...,
        metadata: CallMetadata | None = ...,
        timeout_s: float | None = ...,
    ) -> LLMResponse: ...

    async def generate_structured(
        self,
        messages: list[Msg],
        json_schema: dict[str, Any],
        *,
        route: str = ...,
        system: str | None = ...,
        max_tokens: int = ...,
        allow_fallback: bool = ...,
        metadata: CallMetadata | None = ...,
        schema_name: str = ...,
        timeout_s: float | None = ...,
    ) -> LLMResponse: ...

    async def embed(
        self, inputs: list[str], *, route: str = ..., timeout_s: float | None = ...
    ) -> EmbedResponse: ...


class HttpLLMClient:
    def __init__(
        self,
        base_url: str,
        token: TokenProvider,
        *,
        timeout_s: float = 130.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # timeout_s > gateway deadline (120s), so the gateway's 504 arrives before our timeout.
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)
        self._token = token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(
        self, path: str, body: dict[str, Any], timeout_s: float | None = None
    ) -> dict[str, Any]:
        """`timeout_s` is the caller's deadline: sent to the gateway (which stops its own work
        when it passes) and used for this HTTP call with a small margin, so the gateway's 504
        with a reason arrives before our local timeout."""
        headers = {"Authorization": f"Bearer {await self._token()}"}
        if timeout_s is not None:
            body = {**body, "timeout_ms": max(100, int(timeout_s * 1000))}
        try:
            r = await self._http.post(
                path,
                json=body,
                headers=headers,
                timeout=timeout_s + 5 if timeout_s is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as exc:
            raise LLMError(504, "client-timeout", f"no answer from the LLM gateway: {exc}") from exc
        except httpx.TransportError as exc:
            # one error type for callers: "the LLM path is down", whatever the cause
            raise LLMError(503, "gateway-unreachable", f"{type(exc).__name__}: {exc}") from exc
        if r.status_code >= 400:
            try:
                p = r.json()
            except json.JSONDecodeError:
                p = {}
            raise LLMError(
                r.status_code, p.get("type", "about:blank"), p.get("detail", r.text[:300])
            )
        return r.json()  # type: ignore[no-any-return]

    async def generate(
        self,
        messages: list[Msg],
        *,
        route: str = "reasoning",
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        allow_fallback: bool = True,
        metadata: CallMetadata | None = None,
        timeout_s: float | None = None,
    ) -> LLMResponse:
        body = {
            "route": route,
            "system": system,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "allow_fallback": allow_fallback,
            "metadata": (metadata or CallMetadata()).model_dump(mode="json"),
        }
        return LLMResponse.model_validate(await self._post("/v1/generate", body, timeout_s))

    async def generate_structured(
        self,
        messages: list[Msg],
        json_schema: dict[str, Any],
        *,
        route: str = "reasoning",
        system: str | None = None,
        max_tokens: int = 1024,
        allow_fallback: bool = True,
        metadata: CallMetadata | None = None,
        schema_name: str = "result",
        timeout_s: float | None = None,
    ) -> LLMResponse:
        body = {
            "route": route,
            "system": system,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": max_tokens,
            "json_schema": json_schema,
            "schema_name": schema_name,
            "allow_fallback": allow_fallback,
            "metadata": (metadata or CallMetadata()).model_dump(mode="json"),
        }
        return LLMResponse.model_validate(
            await self._post("/v1/generate_structured", body, timeout_s)
        )

    async def embed(
        self, inputs: list[str], *, route: str = "embed", timeout_s: float | None = None
    ) -> EmbedResponse:
        return EmbedResponse.model_validate(
            await self._post("/v1/embed", {"route": route, "inputs": inputs}, timeout_s)
        )


class FakeLLMClient:
    """Scripted responses for agent unit tests. Records every call for assertions."""

    def __init__(self, texts: list[str] | None = None, data: list[dict[str, Any]] | None = None):
        self.texts = list(texts or [])
        self.data = list(data or [])
        self.calls: list[dict[str, Any]] = []

    def _resp(self, route: str, text: str, data: dict[str, Any] | None) -> LLMResponse:
        return LLMResponse(
            request_id=str(uuid.uuid4()),
            route=route,
            model="fake",
            provider="fake",
            text=text,
            data=data,
            usage=LLMUsage(),
            cost_usd=Decimal(0),
            latency_ms=0,
            fallback_used=False,
            cached=False,
        )

    async def generate(self, messages: list[Msg], **kw: Any) -> LLMResponse:
        self.calls.append({"op": "generate", "messages": messages, **kw})
        return self._resp(
            kw.get("route", "reasoning"), self.texts.pop(0) if self.texts else "ok", None
        )

    async def generate_structured(
        self, messages: list[Msg], json_schema: dict[str, Any], **kw: Any
    ) -> LLMResponse:
        self.calls.append(
            {"op": "generate_structured", "messages": messages, "schema": json_schema, **kw}
        )
        data = self.data.pop(0) if self.data else {}
        return self._resp(kw.get("route", "reasoning"), json.dumps(data), data)

    async def embed(
        self, inputs: list[str], *, route: str = "embed", timeout_s: float | None = None
    ) -> EmbedResponse:
        self.calls.append({"op": "embed", "inputs": inputs})
        return EmbedResponse(
            request_id="fake",
            model="fake",
            dimensions=3,
            vectors=[[1.0, 0.0, 0.0] for _ in inputs],
            usage=LLMUsage(),
            cost_usd=Decimal(0),
            latency_ms=0,
        )


__all__ = [
    "CallMetadata",
    "EmbedResponse",
    "FakeLLMClient",
    "HttpLLMClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMUsage",
    "Msg",
    "TokenProvider",
]
