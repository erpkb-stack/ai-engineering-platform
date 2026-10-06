"""Minimal Server-Sent Events parser for streaming responses (Anthropic and OpenAI style)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEEvent:
    event: str | None
    data: str


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    event: str | None = None
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield SSEEvent(event, "\n".join(data))
            event, data = None, []
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield SSEEvent(event, "\n".join(data))
