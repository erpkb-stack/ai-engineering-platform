"""OpenAI-compatible Chat Completions adapter.

One adapter, many backends - each is just a base_url:
  Ollama (local):  http://localhost:11434/v1      (hosted=False: data stays on the laptop)
  OpenAI:          https://api.openai.com/v1
  Google Gemini:   https://generativelanguage.googleapis.com/v1beta/openai
  vLLM / LM Studio / Azure-compatible proxies.
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
    Usage,
    classify_http_error,
)
from aeoi_llm.providers.sse import iter_sse


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        api_key: str | None,
        timeout_s: float,
        hosted: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.hosted = hosted
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            headers=headers,
            transport=transport,
            # Local servers must not go through a corporate HTTP(S)_PROXY from the env:
            # the proxy can't reach your localhost and answers 502/503 instead.
            trust_env=hosted,
        )
        self._base_url = base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _messages(req: ChatRequest) -> list[dict[str, str]]:
        msgs = [{"role": "system", "content": req.system}] if req.system else []
        return msgs + [{"role": m.role, "content": m.content} for m in req.messages]

    def _body(self, req: ChatRequest, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": self._messages(req),
            "max_tokens": req.max_tokens,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.stop:
            body["stop"] = req.stop
        return body | extra

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name} timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableError(
                f"cannot reach {self._base_url} ({type(exc).__name__}). Is the server running?"
            ) from exc
        if r.status_code != 200:
            raise classify_http_error(r.status_code, r.text, r.headers.get("retry-after"))
        return r.json()  # type: ignore[no-any-return]

    @staticmethod
    def _usage(raw: dict[str, Any] | None) -> Usage:
        if not raw:
            return Usage(estimated=True)
        details = raw.get("prompt_tokens_details") or {}
        return Usage(
            input_tokens=int(raw.get("prompt_tokens", 0)),
            output_tokens=int(raw.get("completion_tokens", 0)),
            cached_tokens=int(details.get("cached_tokens") or 0),
        )

    async def generate(self, req: ChatRequest) -> ChatResult:
        data = await self._post("/chat/completions", self._body(req))
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise RetryableError("malformed completion response") from exc
        return ChatResult(
            text=text,
            usage=self._usage(data.get("usage")),
            model=data.get("model", req.model),
            stop_reason=choice.get("finish_reason"),
        )

    async def generate_structured(self, req: ChatRequest) -> ChatResult:
        if req.json_schema is None:
            raise PermanentError("json_schema is required for structured generation")
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": req.schema_name, "schema": req.json_schema},
        }
        result = await self.generate_with(req, response_format=fmt)
        try:
            data = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RetryableError("model output is not valid JSON") from exc
        if not isinstance(data, dict):
            raise RetryableError("model output JSON is not an object")
        return ChatResult(
            text=result.text,
            usage=result.usage,
            model=result.model,
            stop_reason=result.stop_reason,
            data=data,
        )

    async def generate_with(self, req: ChatRequest, **extra: Any) -> ChatResult:
        data = await self._post("/chat/completions", self._body(req, **extra))
        choice = data["choices"][0]
        return ChatResult(
            text=choice["message"].get("content") or "",
            usage=self._usage(data.get("usage")),
            model=data.get("model", req.model),
            stop_reason=choice.get("finish_reason"),
        )

    async def stream(self, req: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        body = self._body(req, stream=True, stream_options={"include_usage": True})
        usage: Usage | None = None
        model = req.model
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as r:
                if r.status_code != 200:
                    raw = (await r.aread()).decode(errors="replace")
                    raise classify_http_error(r.status_code, raw, r.headers.get("retry-after"))
                async for ev in iter_sse(r.aiter_lines()):
                    if ev.data.strip() == "[DONE]":
                        break
                    payload = json.loads(ev.data)
                    model = payload.get("model", model)
                    if payload.get("usage"):
                        usage = self._usage(payload["usage"])
                    for choice in payload.get("choices", []):
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield StreamChunk(text=text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name} stream timeout") from exc
        yield StreamChunk(usage=usage or Usage(estimated=True), model=model)

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        data = await self._post("/embeddings", {"model": model, "input": inputs})
        try:
            rows = sorted(data["data"], key=lambda d: d["index"])
            vectors = [list(map(float, d["embedding"])) for d in rows]
        except (KeyError, TypeError) as exc:
            raise RetryableError("malformed embeddings response") from exc
        if len(vectors) != len(inputs):
            raise RetryableError("embeddings count does not match inputs")
        return EmbedResult(
            vectors=vectors,
            usage=self._usage(data.get("usage")),
            model=data.get("model", model),
            dimensions=len(vectors[0]) if vectors else 0,
        )
