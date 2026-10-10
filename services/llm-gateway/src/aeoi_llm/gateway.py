"""The gateway core: route -> model chain -> (redact, bulkhead, breaker, retry) -> validate -> record.

Fallback rule (ADR-014): we may degrade, but NEVER silently. Every response says which model
answered (`model`, `fallback_used`, `attempts`). Callers that cannot accept a weaker model
send `allow_fallback=false` and get a 503 instead of a worse answer.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial
from typing import Any, Literal

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from aeoi_common.errors import BadRequestError
from aeoi_llm.cache import ResponseCache, cache_key
from aeoi_llm.config import ModelConfig, ProviderConfig, RoutingConfig
from aeoi_llm.errors import (
    AllProvidersFailedError,
    BudgetExceededError,
    GatewayTimeoutError,
    StructuredOutputError,
    UpstreamRejectedError,
)
from aeoi_llm.providers.base import (
    ChatRequest,
    ChatResult,
    EmbedResult,
    Message,
    PermanentError,
    Provider,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    StreamChunk,
    Usage,
)
from aeoi_llm.resilience import Bulkhead, CircuitBreaker, RetryPolicy, with_retries
from aeoi_llm.usage import UsageRow, UsageSink, cost_usd
from aeoi_security.redaction import redact_text, redact_text_counted

log = structlog.get_logger(__name__)
ChatOp = Literal["generate", "generate_structured", "stream"]
REJECT_STATUSES = frozenset({400, 413, 422})


@dataclass(frozen=True)
class CallMeta:
    prompt_id: str | None = None
    prompt_version: int | None = None
    agent_name: str | None = None
    investigation_id: uuid.UUID | None = None
    caller: str | None = None  # service principal, for logs


@dataclass(frozen=True)
class Attempt:
    model: str
    provider: str
    outcome: Literal["ok", "error", "skipped", "invalid_output"]
    detail: str | None = None
    latency_ms: int = 0


@dataclass
class GatewayResult:
    request_id: str
    route: str
    model: str
    provider: str
    text: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    fallback_used: bool = False
    cached: bool = False
    redactions: int = 0
    repaired: bool = False
    data: dict[str, Any] | None = None
    attempts: list[Attempt] = field(default_factory=list)
    usage_recorded: bool = True


@dataclass
class EmbedOutcome:
    request_id: str
    route: str
    model: str
    provider: str
    vectors: list[list[float]]
    dimensions: int
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    redactions: int = 0


@dataclass
class ProviderRuntime:
    name: str
    config: ProviderConfig
    provider: Provider | None  # None => not configured (e.g. no API key)
    retry: RetryPolicy
    unavailable_reason: str | None = None
    # Breakers are per MODEL: "llama3.2 fails to load" must not block nomic-embed-text
    # on the same Ollama. A dead provider still opens every model's breaker quickly.
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    # Bulkheads are per MODEL too. Regression (Phase 6, owner's Mac): one slow llama3.2 rerank
    # held the single Ollama slot and nomic-embed-text embeddings failed with SaturatedError.
    # Different models are separate queues in Ollama; one must not starve the other.
    bulkheads: dict[str, Bulkhead] = field(default_factory=dict)

    @property
    def hosted(self) -> bool:
        return self.config.hosted

    def breaker_for(self, model: str) -> CircuitBreaker:
        if model not in self.breakers:
            self.breakers[model] = CircuitBreaker(
                f"{self.name}/{model}",
                threshold=self.config.breaker_threshold,
                cooldown_s=self.config.breaker_cooldown_s,
            )
        return self.breakers[model]

    def bulkhead_for(self, model: str) -> Bulkhead:
        if model not in self.bulkheads:
            self.bulkheads[model] = Bulkhead(
                f"{self.name}/{model}", self.config.max_concurrency, self.config.queue_timeout_s
            )
        return self.bulkheads[model]


def _reason(exc: BaseException) -> str:
    """Short, redacted, human-readable cause. A bare class name ('RetryableError') told the
    owner nothing on the first real run; the message ('cannot connect to ...') tells him what to fix."""
    return f"{type(exc).__name__}: {redact_text(str(exc))[:200]}"


def _status_for(exc: ProviderError) -> str:
    if isinstance(exc, RateLimitedError):
        return "RATE_LIMITED"
    if isinstance(exc, ProviderTimeoutError):
        return "TIMEOUT"
    return "ERROR"


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class Gateway:
    def __init__(
        self,
        routing: RoutingConfig,
        runtimes: dict[str, ProviderRuntime],
        sink: UsageSink,
        cache: ResponseCache | None = None,
        *,
        request_timeout_s: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.routing = routing
        self.runtimes = runtimes
        self.sink = sink
        self.cache = cache
        self.request_timeout_s = request_timeout_s
        self._sleep = sleep

    # ------------------------------------------------------------------ helpers

    def _route(self, route: str, operation: Literal["chat", "embed"]) -> list[str]:
        cfg = self.routing.routes.get(route)
        if cfg is None:
            raise BadRequestError(f"Unknown route '{route}'. See GET /v1/routes.")
        if cfg.operation != operation:
            raise BadRequestError(f"Route '{route}' is for '{cfg.operation}', not '{operation}'.")
        return self.routing.chain(route)

    async def _check_budget(self, meta: CallMeta) -> None:
        budget = self.routing.budget_usd_per_investigation
        if budget is None or meta.investigation_id is None:
            return
        spent = await self.sink.spent(meta.investigation_id)
        if spent >= budget:
            raise BudgetExceededError(
                f"Investigation {meta.investigation_id} spent ${spent} of ${budget}."
            )

    @staticmethod
    def _redact(req: ChatRequest) -> tuple[ChatRequest, int]:
        total = 0
        system = req.system
        if system:
            system, n = redact_text_counted(system)
            total += n
        messages = []
        for m in req.messages:
            content, n = redact_text_counted(m.content)
            total += n
            messages.append(Message(m.role, content))
        return replace(req, system=system, messages=messages), total

    async def _guarded[T](
        self, rt: ProviderRuntime, model: str, fn: Callable[[], Awaitable[T]]
    ) -> T:
        """bulkhead -> breaker -> call, wrapped in retries."""
        breaker = rt.breaker_for(model)
        bulkhead = rt.bulkhead_for(model)

        async def once() -> T:
            await bulkhead.acquire()
            try:
                return await breaker.call(fn)
            finally:
                bulkhead.release()

        def on_retry(n: int, exc: BaseException) -> None:
            log.warning("llm_retry", provider=rt.name, model=model, attempt=n, reason=_reason(exc))

        return await with_retries(once, rt.retry, sleep=self._sleep, on_retry=on_retry)

    async def _record(self, row: UsageRow) -> bool:
        return await self.sink.record(row)

    def _row(
        self,
        request_id: str,
        n: int,
        rt: ProviderRuntime,
        model: str,
        op: str,
        status: str,
        latency_ms: int,
        usage: Usage,
        cost: Decimal,
        meta: CallMeta,
        error: str | None = None,
    ) -> UsageRow:
        return UsageRow(
            request_id=f"{request_id}:{n}",
            provider=rt.name,
            model=model,
            operation=op,
            status=status,
            latency_ms=latency_ms,
            usage=usage,
            cost_usd=cost,
            prompt_id=meta.prompt_id,
            prompt_version=meta.prompt_version,
            agent_name=meta.agent_name,
            investigation_id=meta.investigation_id,
            error_type=error,
        )

    # ------------------------------------------------------------------ chat

    async def generate(
        self,
        route: str,
        req: ChatRequest,
        meta: CallMeta,
        *,
        allow_fallback: bool = True,
        use_cache: bool = True,
        deadline_s: float | None = None,
    ) -> GatewayResult:
        return await self._with_deadline(
            self._chat("generate", route, req, meta, allow_fallback, use_cache), deadline_s
        )

    async def generate_structured(
        self,
        route: str,
        req: ChatRequest,
        meta: CallMeta,
        *,
        allow_fallback: bool = True,
        use_cache: bool = True,
        deadline_s: float | None = None,
    ) -> GatewayResult:
        if req.json_schema is None:
            raise BadRequestError("json_schema is required.")
        try:
            Draft202012Validator.check_schema(req.json_schema)
        except SchemaError as exc:
            raise BadRequestError(f"Invalid JSON Schema: {exc.message}") from exc
        if req.json_schema.get("type") != "object":
            raise BadRequestError("The top-level JSON Schema must be an object.")
        return await self._with_deadline(
            self._chat("generate_structured", route, req, meta, allow_fallback, use_cache),
            deadline_s,
        )

    async def _with_deadline[T](self, coro: Awaitable[T], deadline_s: float | None = None) -> T:
        """One deadline for the whole request. The CALLER's deadline wins when it is shorter:
        work the caller has already given up on must stop - otherwise an abandoned 3B-model
        generation keeps the CPU (and the bulkhead slot) busy for minutes."""
        limit = min(self.request_timeout_s, deadline_s) if deadline_s else self.request_timeout_s
        try:
            async with asyncio.timeout(limit):
                return await coro
        except TimeoutError as exc:
            raise GatewayTimeoutError(
                f"No answer within {limit:.1f}s (retries and fallbacks included)."
            ) from exc

    async def _chat(
        self,
        op: ChatOp,
        route: str,
        req: ChatRequest,
        meta: CallMeta,
        allow_fallback: bool,
        use_cache: bool,
    ) -> GatewayResult:
        chain = self._route(route, "chat")
        cap = self.routing.routes[route].max_tokens_cap
        req = replace(req, max_tokens=min(req.max_tokens, cap))
        request_id = str(uuid.uuid4())
        started = time.perf_counter()

        key = None  # cache first: a hit costs $0, so it is allowed even over budget
        # only DETERMINISTIC calls are cached: a model that ignores temperature samples, so its
        # answer must not be replayed as if it were the answer (review finding, Phase 11)
        deterministic = req.temperature == 0 and self.routing.models[chain[0]].supports_temperature
        if self.cache is not None and use_cache and deterministic:
            key = cache_key(op, route, chain, req)
            hit = self.cache.get(key)
            if hit is not None:
                res, info = hit
                return GatewayResult(
                    request_id=request_id,
                    route=route,
                    model=res.model,
                    provider=info["provider"],
                    text=res.text,
                    data=res.data,
                    usage=Usage(),
                    cost_usd=Decimal(0),
                    latency_ms=_ms(started),
                    cached=True,
                    repaired=info.get("repaired", False),
                    redactions=info.get("redactions", 0),
                )

        await self._check_budget(meta)
        candidates = chain if allow_fallback else chain[:1]
        attempts: list[Attempt] = []
        n = 0
        for index, model in enumerate(candidates):
            mcfg = self.routing.models[model]
            rt = self.runtimes[mcfg.provider]
            if rt.provider is None:
                attempts.append(Attempt(model, rt.name, "skipped", rt.unavailable_reason))
                continue
            if op == "generate_structured" and not mcfg.supports_structured:
                attempts.append(Attempt(model, rt.name, "skipped", "no structured output"))
                continue
            call_req = replace(
                req,
                model=model,
                temperature=req.temperature if mcfg.supports_temperature else None,
                force_tool=mcfg.supports_forced_tool,
            )
            redactions = 0
            if rt.hosted:
                call_req, redactions = self._redact(call_req)
            t0 = time.perf_counter()
            n += 1
            try:
                if op == "generate_structured":
                    result, repaired, extra_usage = await self._structured(
                        rt, rt.provider, call_req
                    )
                else:
                    result = await self._guarded(rt, model, partial(rt.provider.generate, call_req))
                    repaired, extra_usage = False, Usage()
            except StructuredOutputError as exc:
                latency = _ms(t0)
                attempts.append(Attempt(model, rt.name, "invalid_output", exc.detail, latency))
                await self._record(
                    self._row(
                        request_id,
                        n,
                        rt,
                        model,
                        op,
                        "ERROR",
                        latency,
                        exc.usage,
                        cost_usd(mcfg, exc.usage),
                        meta,
                        "StructuredOutputError",
                    )
                )
                if index == len(candidates) - 1:
                    raise
                continue
            except ProviderError as exc:
                latency = _ms(t0)
                attempts.append(Attempt(model, rt.name, "error", _reason(exc), latency))
                await self._record(
                    self._row(
                        request_id,
                        n,
                        rt,
                        model,
                        op,
                        _status_for(exc),
                        latency,
                        Usage(),
                        Decimal(0),
                        meta,
                        type(exc).__name__,
                    )
                )
                log.warning(
                    "llm_attempt_failed",
                    route=route,
                    model=model,
                    reason=_reason(exc),
                    status=exc.status,
                    caller=meta.caller,
                )
                if isinstance(exc, PermanentError) and exc.status in REJECT_STATUSES:
                    raise UpstreamRejectedError(
                        f"{rt.name} rejected the request (HTTP {exc.status})."
                    ) from exc
                continue

            latency = _ms(t0)
            usage = _add(result.usage, extra_usage)
            cost = cost_usd(mcfg, usage)
            fallback = index > 0
            attempts.append(Attempt(model, rt.name, "ok", None, latency))
            recorded = await self._record(
                self._row(
                    request_id,
                    n,
                    rt,
                    model,
                    op,
                    "FALLBACK" if fallback else "OK",
                    latency,
                    usage,
                    cost,
                    meta,
                )
            )
            if key is not None and self.cache is not None and not fallback:
                self.cache.put(
                    key,
                    result,
                    {"provider": rt.name, "repaired": repaired, "redactions": redactions},
                )
            log.info(
                "llm_call",
                route=route,
                model=model,
                op=op,
                fallback=fallback,
                latency_ms=latency,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=str(cost),
                caller=meta.caller,
                agent=meta.agent_name,
                redactions=redactions,
            )
            return GatewayResult(
                request_id=request_id,
                route=route,
                model=result.model or model,
                provider=rt.name,
                text=result.text,
                data=result.data,
                usage=usage,
                cost_usd=cost,
                latency_ms=_ms(started),
                fallback_used=fallback,
                redactions=redactions,
                repaired=repaired,
                attempts=attempts,
                usage_recorded=recorded,
            )

        raise AllProvidersFailedError(
            f"Route '{route}' failed: "
            + "; ".join(f"{a.model}={a.outcome}({a.detail})" for a in attempts),
            errors=[a.__dict__ for a in attempts],
        )

    async def _structured(
        self, rt: ProviderRuntime, provider: Provider, req: ChatRequest
    ) -> tuple[ChatResult, bool, Usage]:
        """Call, validate against the schema, and allow ONE repair round-trip."""
        validator = Draft202012Validator(req.json_schema or {})
        result = await self._guarded(rt, req.model, lambda: provider.generate_structured(req))
        errors = _schema_errors(validator, result.data)
        if not errors:
            return result, False, Usage()

        repair_req = replace(
            req,
            messages=[
                *req.messages,
                Message("assistant", result.text[:8000]),
                Message(
                    "user",
                    "Your output failed JSON Schema validation:\n- "
                    + "\n- ".join(errors)
                    + "\nReturn the corrected object only. Do not add fields.",
                ),
            ],
        )
        second = await self._guarded(
            rt, req.model, lambda: provider.generate_structured(repair_req)
        )
        errors = _schema_errors(validator, second.data)
        if errors:
            raise StructuredOutputError(
                f"Still invalid after one repair: {'; '.join(errors[:3])}",
                usage=_add(result.usage, second.usage),
            )
        return second, True, result.usage

    # ------------------------------------------------------------------ stream

    async def stream(
        self, route: str, req: ChatRequest, meta: CallMeta, *, allow_fallback: bool = True
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yields {"event": "meta"|"delta"|"done"|"error", ...}.

        Fallback is possible only BEFORE the first token: once text reached the user we cannot
        switch models mid-sentence. A mid-stream failure ends with an `error` event.
        """
        chain = self._route(route, "chat")
        req = replace(
            req, max_tokens=min(req.max_tokens, self.routing.routes[route].max_tokens_cap)
        )
        await self._check_budget(meta)
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        candidates = chain if allow_fallback else chain[:1]
        attempts: list[Attempt] = []
        n = 0
        for index, model in enumerate(candidates):
            mcfg = self.routing.models[model]
            rt = self.runtimes[mcfg.provider]
            if rt.provider is None:
                attempts.append(Attempt(model, rt.name, "skipped", rt.unavailable_reason))
                continue
            call_req = replace(
                req,
                model=model,
                temperature=req.temperature if mcfg.supports_temperature else None,
                force_tool=mcfg.supports_forced_tool,
            )
            redactions = 0
            if rt.hosted:
                call_req, redactions = self._redact(call_req)
            n += 1
            t0 = time.perf_counter()
            try:
                agen, first = await self._open_stream(rt, rt.provider, call_req)
            except ProviderError as exc:
                attempts.append(Attempt(model, rt.name, "error", _reason(exc), _ms(t0)))
                await self._record(
                    self._row(
                        request_id,
                        n,
                        rt,
                        model,
                        "stream",
                        _status_for(exc),
                        _ms(t0),
                        Usage(),
                        Decimal(0),
                        meta,
                        type(exc).__name__,
                    )
                )
                if isinstance(exc, PermanentError) and exc.status in REJECT_STATUSES:
                    yield {
                        "event": "error",
                        "type": "llm-request-rejected",
                        "detail": str(exc.status),
                    }
                    return
                continue

            fallback = index > 0
            yield {
                "event": "meta",
                "request_id": request_id,
                "model": model,
                "provider": rt.name,
                "fallback_used": fallback,
                "redactions": redactions,
            }
            usage = Usage(estimated=True)
            status, error = ("FALLBACK" if fallback else "OK"), None
            try:
                chunk: StreamChunk | None = first
                while chunk is not None:
                    if chunk.text:
                        yield {"event": "delta", "text": chunk.text}
                    if chunk.usage is not None:
                        usage = chunk.usage
                    chunk = await anext(agen, None)
            except ProviderError as exc:
                status, error = _status_for(exc), type(exc).__name__
                yield {"event": "error", "type": "llm-stream-interrupted", "detail": error}
            finally:
                await agen.aclose()
                rt.bulkhead_for(model).release()
                cost = cost_usd(mcfg, usage)
                await self._record(
                    self._row(
                        request_id,
                        n,
                        rt,
                        model,
                        "stream",
                        status,
                        _ms(t0),
                        usage,
                        cost,
                        meta,
                        error,
                    )
                )
            if error is None:
                yield {
                    "event": "done",
                    "usage": usage.__dict__,
                    "cost_usd": str(cost),
                    "latency_ms": _ms(started),
                }
            return
        yield {
            "event": "error",
            "type": "llm-unavailable",
            "detail": "; ".join(f"{a.model}={a.outcome}({a.detail})" for a in attempts),
        }

    async def _open_stream(
        self, rt: ProviderRuntime, provider: Provider, req: ChatRequest
    ) -> tuple[AsyncGenerator[StreamChunk, None], StreamChunk]:
        """Retry/breaker cover only 'getting the first chunk'. The bulkhead slot is held until
        the caller finishes the stream (released in stream()'s finally)."""

        async def first_chunk() -> tuple[AsyncGenerator[StreamChunk, None], StreamChunk]:
            agen = provider.stream(req)
            try:
                first = await anext(agen)
            except BaseException:
                await agen.aclose()
                raise
            return agen, first

        bulkhead = rt.bulkhead_for(req.model)

        async def once() -> tuple[AsyncGenerator[StreamChunk, None], StreamChunk]:
            await bulkhead.acquire()
            try:
                return await rt.breaker_for(req.model).call(first_chunk)
            except BaseException:
                bulkhead.release()
                raise

        return await with_retries(once, rt.retry, sleep=self._sleep)

    # ------------------------------------------------------------------ embed

    async def embed(
        self, route: str, inputs: list[str], meta: CallMeta, *, deadline_s: float | None = None
    ) -> EmbedOutcome:
        chain = self._route(route, "embed")
        model = chain[0]  # embed routes have no fallbacks (validated at load)
        mcfg: ModelConfig = self.routing.models[model]
        rt = self.runtimes[mcfg.provider]
        if rt.provider is None:
            raise AllProvidersFailedError(
                f"Embedding provider '{rt.name}': {rt.unavailable_reason}"
            )
        redactions = 0
        if rt.hosted:
            cleaned = []
            for text in inputs:
                t, k = redact_text_counted(text)
                cleaned.append(t)
                redactions += k
            inputs = cleaned
        request_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        provider = rt.provider
        try:
            res: EmbedResult = await self._with_deadline(
                self._guarded(rt, model, lambda: provider.embed(model, inputs)), deadline_s
            )
        except ProviderError as exc:
            await self._record(
                self._row(
                    request_id,
                    1,
                    rt,
                    model,
                    "embed",
                    _status_for(exc),
                    _ms(t0),
                    Usage(),
                    Decimal(0),
                    meta,
                    type(exc).__name__,
                )
            )
            if isinstance(exc, PermanentError) and exc.status in REJECT_STATUSES:
                raise UpstreamRejectedError(f"{rt.name} rejected the embedding request.") from exc
            raise AllProvidersFailedError(f"Embedding failed: {_reason(exc)}") from exc
        if res.dimensions != mcfg.dimensions:
            # Wrong model pulled / config drift: refuse rather than write bad vectors.
            raise AllProvidersFailedError(
                f"Model '{model}' returned {res.dimensions}-d vectors; config says {mcfg.dimensions}."
            )
        cost = cost_usd(mcfg, res.usage)
        await self._record(
            self._row(request_id, 1, rt, model, "embed", "OK", _ms(t0), res.usage, cost, meta)
        )
        return EmbedOutcome(
            request_id=request_id,
            route=route,
            model=model,
            provider=rt.name,
            vectors=res.vectors,
            dimensions=res.dimensions,
            usage=res.usage,
            cost_usd=cost,
            latency_ms=_ms(t0),
            redactions=redactions,
        )

    # ------------------------------------------------------------------ introspection

    def describe(self) -> dict[str, Any]:
        return {
            "routes": {
                name: {
                    "operation": r.operation,
                    "chain": self.routing.chain(name),
                    "max_tokens_cap": r.max_tokens_cap,
                    "description": r.description,
                }
                for name, r in self.routing.routes.items()
            },
            "models": {name: m.provider for name, m in self.routing.models.items()},
            "providers": {
                name: {
                    "kind": rt.config.kind.value,
                    # local endpoints only: a hosted URL is public anyway, but keep output minimal
                    "base_url": None if rt.hosted else rt.config.base_url,
                    "hosted": rt.hosted,
                    "configured": rt.provider is not None,
                    "unavailable_reason": rt.unavailable_reason,
                    "breakers": {m: b.state.value for m, b in rt.breakers.items()},
                    "in_flight": {m: b.in_flight for m, b in rt.bulkheads.items()},
                    "max_concurrency_per_model": rt.config.max_concurrency,
                }
                for name, rt in self.runtimes.items()
            },
            "budget_usd_per_investigation": (
                str(self.routing.budget_usd_per_investigation)
                if self.routing.budget_usd_per_investigation is not None
                else None
            ),
            "pricing_note": self.routing.pricing_note,
            "cache": None
            if self.cache is None
            else {"entries": len(self.cache), "hits": self.cache.hits, "misses": self.cache.misses},
        }

    async def aclose(self) -> None:
        for rt in self.runtimes.values():
            if rt.provider is not None:
                await rt.provider.aclose()


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.cached_tokens + b.cached_tokens,
        a.estimated or b.estimated,
    )


def _schema_errors(validator: Draft202012Validator, data: dict[str, Any] | None) -> list[str]:
    if data is None:
        return ["output is not a JSON object"]
    errs = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errs[:10]]


def dumps(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str, separators=(",", ":"))
