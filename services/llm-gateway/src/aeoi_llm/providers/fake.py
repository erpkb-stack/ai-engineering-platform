"""Deterministic provider for tests and offline demos. Never calls the network.

- generate: scripted replies (FIFO) or a stable echo.
- structured: scripted dicts, or a minimal object built from the schema's required fields.
- embed: stable pseudo-vectors from a hash (same text -> same vector).
- fail_next: make the next N calls raise a given error (to test retry/fallback).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from aeoi_llm.providers.base import (
    ChatRequest,
    ChatResult,
    EmbedResult,
    ProviderError,
    StreamChunk,
    Usage,
)


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _minimal(schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties", {})
        return {k: _minimal(props.get(k, {})) for k in schema.get("required", [])}
    if t == "array":
        return [_minimal(schema.get("items", {}))] * schema.get("minItems", 0)
    if t == "integer":
        return int(schema.get("minimum", 0))
    if t == "number":
        return float(schema.get("minimum", 0))
    if t == "boolean":
        return False
    if t == "string":
        return "x" * int(schema.get("minLength", 1))
    return None


@dataclass
class FakeProvider:
    name: str = "fake"
    hosted: bool = False
    dimensions: int = 768
    replies: list[str] = field(default_factory=list)
    structured: list[dict[str, Any]] = field(default_factory=list)
    errors: list[ProviderError] = field(default_factory=list)
    calls: list[ChatRequest] = field(default_factory=list)

    def fail_next(self, *errors: ProviderError) -> None:
        self.errors.extend(errors)

    def _maybe_fail(self) -> None:
        if self.errors:
            raise self.errors.pop(0)

    async def aclose(self) -> None:
        return None

    async def generate(self, req: ChatRequest) -> ChatResult:
        self.calls.append(req)
        self._maybe_fail()
        last = req.messages[-1].content if req.messages else ""
        text = self.replies.pop(0) if self.replies else f"[fake:{req.model}] {last[:200]}"
        prompt = (req.system or "") + "".join(m.content for m in req.messages)
        return ChatResult(
            text=text,
            usage=Usage(_tokens(prompt), _tokens(text)),
            model=req.model,
            stop_reason="end_turn",
        )

    async def generate_structured(self, req: ChatRequest) -> ChatResult:
        self.calls.append(req)
        self._maybe_fail()
        data = self.structured.pop(0) if self.structured else _minimal(req.json_schema or {})
        import json

        text = json.dumps(data)
        return ChatResult(text=text, usage=Usage(50, _tokens(text)), model=req.model, data=data)

    async def stream(self, req: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        result = await self.generate(req)
        for word in result.text.split(" "):
            yield StreamChunk(text=word + " ")
        yield StreamChunk(usage=result.usage, model=req.model)

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        """Hashed bag-of-words vectors: deterministic and LEXICALLY meaningful (texts sharing
        words are close), so retrieval tests in CI exercise real ranking code. Not semantic:
        a paraphrase with no shared words scores ~0. Never quote recall measured with this."""
        self._maybe_fail()
        vectors = [_hashed_bow(text, self.dimensions) for text in inputs]
        return EmbedResult(
            vectors=vectors,
            usage=Usage(sum(_tokens(t) for t in inputs), 0),
            model=model,
            dimensions=self.dimensions,
        )


_WORD = re.compile(r"[a-z0-9]+")


def _hashed_bow(text: str, dims: int) -> list[float]:
    vec = [0.0] * dims
    for word in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
        vec[h % dims] += 1.0 if (h >> 32) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        vec[0], norm = 1.0, 1.0  # empty text: fixed unit vector instead of a zero vector
    return [v / norm for v in vec]
