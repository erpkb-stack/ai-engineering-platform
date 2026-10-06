"""HTTP API. Callers are SERVICES (orchestrator, rag, evaluation) with scope `llm:invoke`.

End users never call this directly: user tokens get 403 (require_scope rejects them).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aeoi_llm.gateway import CallMeta, Gateway, GatewayResult, dumps
from aeoi_llm.providers.base import ChatRequest, Message
from aeoi_security.auth import Principal
from aeoi_web import require_scope

SCOPE = "llm:invoke"
router = APIRouter(prefix="/v1", tags=["llm"])
Caller = Annotated[Principal, Depends(require_scope(SCOPE))]


def _gateway(request: Request) -> Gateway:
    gw: Gateway = request.app.state.gateway
    return gw


GatewayDep = Annotated[Gateway, Depends(_gateway)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageIn(_In):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=400_000)


class Metadata(_In):
    prompt_id: str | None = Field(default=None, max_length=80)
    prompt_version: int | None = Field(default=None, ge=1)
    agent_name: str | None = Field(default=None, max_length=64)
    investigation_id: uuid.UUID | None = None


class GenerateIn(_In):
    route: str = Field(default="reasoning", max_length=40)
    system: str | None = Field(default=None, max_length=200_000)
    messages: list[MessageIn] = Field(min_length=1, max_length=200)
    max_tokens: int = Field(default=1024, ge=1, le=32_000)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    stop: list[str] | None = Field(default=None, max_length=4)
    allow_fallback: bool = True
    cache: bool = True
    # The caller's own deadline. The gateway stops all work (retries, fallback, the upstream
    # call) when it passes, instead of finishing an answer nobody is waiting for.
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    metadata: Metadata = Field(default_factory=Metadata)

    def to_request(self) -> ChatRequest:
        return ChatRequest(
            model="",
            system=self.system,
            messages=[Message(m.role, m.content) for m in self.messages],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stop=self.stop,
        )


class StructuredIn(GenerateIn):
    json_schema: dict[str, Any]
    schema_name: str = Field(default="result", pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class EmbedIn(_In):
    route: str = Field(default="embed", max_length=40)
    inputs: list[str] = Field(min_length=1, max_length=256)
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    metadata: Metadata = Field(default_factory=Metadata)


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated: bool


class AttemptOut(BaseModel):
    model: str
    provider: str
    outcome: str
    detail: str | None
    latency_ms: int


class GenerateOut(BaseModel):
    request_id: str
    route: str
    model: str
    provider: str
    text: str
    data: dict[str, Any] | None = None
    usage: UsageOut
    cost_usd: Decimal
    latency_ms: int
    fallback_used: bool
    cached: bool
    repaired: bool
    redactions: int
    usage_recorded: bool
    attempts: list[AttemptOut]


class EmbedOut(BaseModel):
    request_id: str
    model: str
    dimensions: int
    vectors: list[list[float]]
    usage: UsageOut
    cost_usd: Decimal
    latency_ms: int


def _secs(ms: int | None) -> float | None:
    return ms / 1000 if ms else None


def _meta(m: Metadata, caller: Principal) -> CallMeta:
    return CallMeta(
        prompt_id=m.prompt_id,
        prompt_version=m.prompt_version,
        agent_name=m.agent_name,
        investigation_id=m.investigation_id,
        caller=caller.actor,
    )


def _out(r: GatewayResult) -> GenerateOut:
    return GenerateOut(
        request_id=r.request_id,
        route=r.route,
        model=r.model,
        provider=r.provider,
        text=r.text,
        data=r.data,
        usage=UsageOut(**r.usage.__dict__),
        cost_usd=r.cost_usd,
        latency_ms=r.latency_ms,
        fallback_used=r.fallback_used,
        cached=r.cached,
        repaired=r.repaired,
        redactions=r.redactions,
        usage_recorded=r.usage_recorded,
        attempts=[AttemptOut(**a.__dict__) for a in r.attempts],
    )


@router.post("/generate", response_model=GenerateOut)
async def generate(body: GenerateIn, caller: Caller, gw: GatewayDep) -> GenerateOut:
    result = await gw.generate(
        body.route,
        body.to_request(),
        _meta(body.metadata, caller),
        allow_fallback=body.allow_fallback,
        use_cache=body.cache,
        deadline_s=_secs(body.timeout_ms),
    )
    return _out(result)


@router.post("/generate_structured", response_model=GenerateOut)
async def generate_structured(body: StructuredIn, caller: Caller, gw: GatewayDep) -> GenerateOut:
    req = body.to_request()
    req = ChatRequest(
        **{**req.__dict__, "json_schema": body.json_schema, "schema_name": body.schema_name}
    )
    result = await gw.generate_structured(
        body.route,
        req,
        _meta(body.metadata, caller),
        allow_fallback=body.allow_fallback,
        use_cache=body.cache,
        deadline_s=_secs(body.timeout_ms),
    )
    return _out(result)


@router.post("/stream")
async def stream(body: GenerateIn, caller: Caller, gw: GatewayDep) -> StreamingResponse:
    """Server-Sent Events: meta -> delta* -> done | error."""
    events = gw.stream(
        body.route,
        body.to_request(),
        _meta(body.metadata, caller),
        allow_fallback=body.allow_fallback,
    )
    # Pull the first event BEFORE returning 200, so route/budget errors become proper
    # problem+json responses instead of an SSE stream that starts and immediately dies.
    first = await anext(events)

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            for ev in (first,):
                yield _sse(ev)
            async for ev in events:
                yield _sse(ev)
        finally:
            await events.aclose()

    return StreamingResponse(
        body_iter(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(ev: dict[str, Any]) -> bytes:
    name = ev.pop("event")
    return f"event: {name}\ndata: {dumps(ev)}\n\n".encode()


@router.post("/embed", response_model=EmbedOut)
async def embed(body: EmbedIn, caller: Caller, gw: GatewayDep) -> EmbedOut:
    r = await gw.embed(
        body.route, body.inputs, _meta(body.metadata, caller), deadline_s=_secs(body.timeout_ms)
    )
    return EmbedOut(
        request_id=r.request_id,
        model=r.model,
        dimensions=r.dimensions,
        vectors=r.vectors,
        usage=UsageOut(**r.usage.__dict__),
        cost_usd=r.cost_usd,
        latency_ms=r.latency_ms,
    )


@router.get("/routes")
async def routes(_: Caller, gw: GatewayDep) -> dict[str, Any]:
    return gw.describe()
