"""Anthropic Messages API adapter (raw httpx: full control of retries/timeouts, easy to mock).

- generate:            POST /v1/messages
- generate_structured: forced tool use (tool_choice = that tool) with the JSON Schema as the
                       tool's input_schema -> the model must return arguments matching it.
- stream:              same request with "stream": true, Server-Sent Events.
- embed:               not offered by Anthropic -> UnsupportedOperationError (embeddings route to a
                       local model; ADR-014).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from aeoi_llm.providers.base import (
    ChatRequest,
    ChatResult,
    EmbedResult,
    PermanentError,
    ProviderTimeoutError,
    RetryableError,
    StreamChunk,
    UnsupportedOperationError,
    Usage,
    classify_http_error,
)
from aeoi_llm.providers.sse import iter_sse

API_VERSION = "2023-06-01"


class AnthropicProvider:
    hosted = True

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self, req: ChatRequest, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.system:
            body["system"] = req.system
        if req.stop:
            body["stop_sequences"] = req.stop
        return body | extra

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post("/v1/messages", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("anthropic timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableError(f"cannot reach Anthropic API ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise classify_http_error(r.status_code, r.text, r.headers.get("retry-after"))
        return r.json()  # type: ignore[no-any-return]

    @staticmethod
    def _usage(raw: dict[str, Any]) -> Usage:
        # Anthropic reports cache reads/writes SEPARATELY from input_tokens. We normalise to
        # "input_tokens = all prompt tokens, cached_tokens = the part read from cache"
        # (the OpenAI convention) so cost code has one formula for every vendor.
        cached = int(raw.get("cache_read_input_tokens") or 0)
        written = int(raw.get("cache_creation_input_tokens") or 0)
        return Usage(
            input_tokens=int(raw.get("input_tokens", 0)) + cached + written,
            output_tokens=int(raw.get("output_tokens", 0)),
            cached_tokens=cached,
        )

    async def generate(self, req: ChatRequest) -> ChatResult:
        data = await self._post(self._body(req))
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        return ChatResult(
            text=text,
            usage=self._usage(data.get("usage", {})),
            model=data.get("model", req.model),
            stop_reason=data.get("stop_reason"),
        )

    async def generate_structured(self, req: ChatRequest) -> ChatResult:
        if req.json_schema is None:
            raise PermanentError("json_schema is required for structured generation")
        tool = {
            "name": req.schema_name,
            "description": "Return the result. Arguments must match the schema exactly.",
            "input_schema": req.json_schema,
        }
        if req.force_tool:
            body = self._body(
                req, tools=[tool], tool_choice={"type": "tool", "name": req.schema_name}
            )
        else:
            # the model refuses forced tool use: one tool, `auto`, and a plain instruction. If it
            # answers in text instead, the RetryableError below lets the gateway retry/repair.
            must = f"Answer ONLY by calling the tool `{req.schema_name}` exactly once."
            body = self._body(req, tools=[tool], tool_choice={"type": "auto"})
            body["system"] = f"{body['system']}\n\n{must}" if body.get("system") else must
        data = await self._post(body)
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == req.schema_name:
                args = block.get("input")
                if isinstance(args, dict):
                    return ChatResult(
                        text=json.dumps(args),
                        usage=self._usage(data.get("usage", {})),
                        model=data.get("model", req.model),
                        stop_reason=data.get("stop_reason"),
                        data=args,
                    )
        raise RetryableError("model did not return the forced tool call")

    async def stream(self, req: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        body = self._body(req, stream=True)
        input_tokens = output_tokens = cached = 0
        model = req.model
        try:
            async with self._client.stream("POST", "/v1/messages", json=body) as r:
                if r.status_code != 200:
                    raw = (await r.aread()).decode(errors="replace")
                    raise classify_http_error(r.status_code, raw, r.headers.get("retry-after"))
                async for ev in iter_sse(r.aiter_lines()):
                    payload = json.loads(ev.data) if ev.data else {}
                    kind = payload.get("type", ev.event)
                    if kind == "message_start":
                        msg = payload.get("message", {})
                        model = msg.get("model", model)
                        start = self._usage(msg.get("usage", {}))
                        input_tokens, cached = start.input_tokens, start.cached_tokens
                    elif kind == "content_block_delta":
                        delta = payload.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield StreamChunk(text=delta["text"])
                    elif kind == "message_delta":
                        output_tokens = int(
                            payload.get("usage", {}).get("output_tokens", output_tokens)
                        )
                    elif kind == "error":
                        err = payload.get("error", {})
                        raise RetryableError(
                            f"stream error: {err.get('type')}: {err.get('message')}"
                        )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("anthropic stream timeout") from exc
        yield StreamChunk(usage=Usage(input_tokens, output_tokens, cached), model=model)

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        raise UnsupportedOperationError(
            "Anthropic has no embeddings API; route 'embed' to a local model"
        )
