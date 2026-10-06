"""Resilience primitives shared by every service that calls something remote:
retry with full jitter, circuit breaker, bulkhead (bounded concurrency).

Moved here from the LLM gateway in Phase 7 when the Tool Gateway needed the same thing - one
tested implementation instead of two drifting copies. Dependency-free on purpose (no
tenacity/pybreaker): ~120 lines with a fake clock in the tests.

Contract: raise (a subclass of) `TransientError` for failures worth retrying and worth
counting against a dependency's health. Anything else is raised at once and does not trip
the breaker (a bad request says nothing about the dependency).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TransientError(Exception):
    """Retryable. May carry `retry_after_s` (e.g. from HTTP 429 Retry-After)."""


class CircuitOpenError(TransientError):
    """Fast failure: the dependency is known-bad; don't spend the caller's deadline on it."""


class SaturatedError(TransientError):
    """Bulkhead full: too many in-flight calls to this dependency."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    # If the dependency says "retry after 40s", waiting is worse than failing over.
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
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Retry only TransientError; everything else is raised at once."""
    rng = rng or random.Random()  # noqa: S311 - jitter, not crypto
    attempt = 0
    while True:
        try:
            return await call()
        except TransientError as exc:
            attempt += 1
            if attempt >= policy.max_attempts:
                raise
            wait = policy.delay(attempt, rng)
            retry_after = getattr(exc, "retry_after_s", None)
            if retry_after is not None:
                if retry_after > policy.max_retry_after_s:
                    raise  # fail over instead of stalling the caller
                wait = max(wait, float(retry_after))
            if on_retry is not None:
                on_retry(attempt, exc)
            await sleep(wait)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive transient failures; one trial call after `cooldown_s`."""

    name: str
    threshold: int = 5
    cooldown_s: float = 30.0
    clock: Clock = time.monotonic
    open_error: ClassVar[type[Exception]] = CircuitOpenError
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
                raise self.open_error(f"circuit open for provider '{self.name}'")
            if state is BreakerState.HALF_OPEN:
                self._trial_in_flight = True
        try:
            result = await fn()
        except TransientError:
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
            self._opened_at = self.clock()  # (re)open; a failed trial restarts the cooldown


class Bulkhead:
    """Bounded concurrency with a bounded wait. Queueing work behind a slow dependency just
    converts load into timeouts."""

    saturated_error: ClassVar[type[Exception]] = SaturatedError

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
            raise self.saturated_error(
                f"provider '{self.name}' saturated ({self.limit} in flight)"
            ) from exc
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight -= 1
        self._sem.release()
