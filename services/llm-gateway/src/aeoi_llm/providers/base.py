"""Provider-neutral types. Adapters translate these to/from each vendor's wire format."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from aeoi_common.resilience import TransientError

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[Message]
    system: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.0
    stop: list[str] | None = None
    # For generate_structured: the JSON Schema the output must satisfy.
    json_schema: dict[str, Any] | None = None
    schema_name: str = "result"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    estimated: bool = False  # True when the provider didn't report usage (e.g. some streams)


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: Usage
    model: str  # model the provider says it used
    stop_reason: str | None = None
    data: dict[str, Any] | None = None  # parsed JSON for structured calls


@dataclass(frozen=True)
class StreamChunk:
    text: str = ""
    usage: Usage | None = None  # set on the final chunk
    model: str | None = None


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    usage: Usage
    model: str
    dimensions: int = field(default=0)


class ProviderError(Exception):
    """Base. `retryable` decides retry/fallback; `status` is the upstream HTTP status if any."""

    retryable = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RetryableError(ProviderError, TransientError):
    retryable = True


class RateLimitedError(RetryableError):
    def __init__(
        self, message: str, *, retry_after_s: float | None = None, status: int = 429
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after_s = retry_after_s


class ProviderTimeoutError(RetryableError):
    pass


class PermanentError(ProviderError):
    """Bad request / auth / unsupported: retrying the same call cannot succeed."""


class UnsupportedOperationError(PermanentError):
    pass


class Provider(Protocol):
    name: str
    hosted: bool  # True = data leaves the machine (secrets are scrubbed before sending)

    async def generate(self, req: ChatRequest) -> ChatResult: ...
    async def generate_structured(self, req: ChatRequest) -> ChatResult: ...
    def stream(self, req: ChatRequest) -> AsyncGenerator[StreamChunk, None]: ...
    async def embed(self, model: str, inputs: list[str]) -> EmbedResult: ...
    async def aclose(self) -> None: ...


def classify_http_error(status: int, body: str, retry_after: str | None) -> ProviderError:
    """Map vendor HTTP status codes to retry semantics (same rules for every vendor)."""
    snippet = body[:300]
    if status == 429:
        return RateLimitedError(f"rate limited: {snippet}", retry_after_s=_seconds(retry_after))
    if status in (408, 409, 500, 502, 503, 504, 529):  # 529 = Anthropic "overloaded"
        return RetryableError(f"upstream {status}: {snippet}", status=status)
    return PermanentError(f"upstream {status}: {snippet}", status=status)


def _seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
