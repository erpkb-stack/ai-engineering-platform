"""Resilience primitives: retry with full jitter, circuit breaker, bulkhead (concurrency limit).

Kept dependency-free on purpose (no tenacity/pybreaker): ~100 lines we can test with a fake
clock beat two libraries whose async semantics we'd have to learn and pin.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from aeoi_llm.providers.base import ProviderError, RateLimitedError, RetryableError

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    # If the provider says "retry after 40s", waiting is worse than falling back.
    max_retry_after_s: float = 10.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Full jitter (AWS architecture blog): uniform(0, min(cap, base * 2^attempt))."""
        return rng.uniform(0, min(self.max_delay_s, self.base_delay_s * 2**attempt))


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, ProviderError], None] | None = None,
) -> T:
    """Retry only RetryableError. PermanentError (400/401/403/404) is raised at once."""
    rng = rng or random.Random()  # noqa: S311 - jitter, not crypto
    attempt = 0
    while True:
        try:
            return await call()
        except RetryableError as exc:
            attempt += 1
            if attempt >= policy.max_attempts:
                raise
            wait = policy.delay(attempt, rng)
            if isinstance(exc, RateLimitedError) and exc.retry_after_s is not None:
                if exc.retry_after_s > policy.max_retry_after_s:
                    raise  # let the router fall back instead of stalling the caller
                wait = max(wait, exc.retry_after_s)
            if on_retry is not None:
                on_retry(attempt, exc)
            await sleep(wait)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RetryableError):
    """Fast failure: the provider is known-bad; don't spend the caller's deadline on it."""


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive retryable failures; one trial call after `cooldown_s`.

    Only RETRYABLE failures count. A 400 from a bad prompt says nothing about provider health.
    """

    name: str
    threshold: int = 5
    cooldown_s: float = 30.0
    clock: Clock = time.monotonic
    _failures: int = 0
    _opened_at: float | None = None
    _trial_in_flight: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self.clock() - self._opened_at >= self.cooldown_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    async def call[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            state = self.state
            if state is BreakerState.OPEN or (
                state is BreakerState.HALF_OPEN and self._trial_in_flight
            ):
                raise CircuitOpenError(f"circuit open for provider '{self.name}'")
            if state is BreakerState.HALF_OPEN:
                self._trial_in_flight = True
        try:
            result = await fn()
        except RetryableError:
            self._record_failure()
            raise
        except BaseException:
            self._trial_in_flight = False  # permanent errors / cancellation: not a health signal
            raise
        self._record_success()
        return result

    def _record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def _record_failure(self) -> None:
        self._trial_in_flight = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self.threshold:
            self._opened_at = self.clock()  # (re)open; half-open trial failure restarts cooldown


class SaturatedError(RetryableError):
    """Bulkhead full: too many in-flight calls to this provider."""


class Bulkhead:
    """Concurrency limit per provider. Ollama on an Intel CPU handles ~1 request at a time;
    queueing 20 behind it just converts load into timeouts. Waiting is bounded."""

    def __init__(self, name: str, limit: int, queue_timeout_s: float) -> None:
        self.name = name
        self.limit = limit
        self.queue_timeout_s = queue_timeout_s
        self._sem = asyncio.Semaphore(limit)
        self.in_flight = 0

    async def acquire(self) -> None:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.queue_timeout_s)
        except TimeoutError as exc:
            raise SaturatedError(
                f"provider '{self.name}' saturated ({self.limit} in flight)"
            ) from exc
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight -= 1
        self._sem.release()
