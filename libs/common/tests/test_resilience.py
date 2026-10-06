"""Shared resilience primitives (moved here from the LLM gateway in Phase 7). The gateway's own
tests cover its subclasses; these pin the generic contract both services rely on."""

from __future__ import annotations

import asyncio
import random

import pytest

from aeoi_common.resilience import (
    BreakerState,
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    SaturatedError,
    TransientError,
    with_retries,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _no_sleep(_: float) -> None:
    return None


async def test_only_transient_errors_are_retried() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientError("blip")
        return "ok"

    assert await with_retries(flaky, RetryPolicy(max_attempts=3), sleep=_no_sleep) == "ok"

    async def bad() -> str:
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        await with_retries(bad, RetryPolicy(max_attempts=5), sleep=_no_sleep)


async def test_retry_after_longer_than_cap_fails_fast() -> None:
    err = TransientError("429")
    err.retry_after_s = 60  # type: ignore[attr-defined]
    calls = 0

    async def limited() -> None:
        nonlocal calls
        calls += 1
        raise err

    with pytest.raises(TransientError):
        await with_retries(
            limited, RetryPolicy(max_attempts=3, max_retry_after_s=10), sleep=_no_sleep
        )
    assert calls == 1


def test_full_jitter_is_bounded() -> None:
    p, rng = RetryPolicy(base_delay_s=0.5, max_delay_s=2.0), random.Random(1)
    assert all(0 <= p.delay(a, rng) <= 2.0 for a in range(1, 20))


async def test_breaker_opens_ignores_permanent_errors_and_half_opens_once() -> None:
    clock = Clock()
    b = CircuitBreaker("dep", threshold=2, cooldown_s=10, clock=clock)

    async def fail() -> None:
        raise TransientError("down")

    async def permanent() -> None:
        raise ValueError("bad request")

    for _ in range(5):
        with pytest.raises(ValueError, match="bad request"):
            await b.call(permanent)
    assert b.state is BreakerState.CLOSED  # a bad request says nothing about the dependency
    for _ in range(2):
        with pytest.raises(TransientError):
            await b.call(fail)
    assert b.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await b.call(fail)
    clock.t = 11
    assert b.state is BreakerState.HALF_OPEN
    gate = asyncio.Event()

    async def slow_ok() -> str:
        await gate.wait()
        return "ok"

    trial = asyncio.create_task(b.call(slow_ok))
    await asyncio.sleep(0)
    with pytest.raises(CircuitOpenError):  # only ONE trial call while half-open
        await b.call(slow_ok)
    gate.set()
    assert await trial == "ok" and b.state is BreakerState.CLOSED


async def test_bulkhead_rejects_instead_of_queueing_forever() -> None:
    bh = Bulkhead("dep", limit=1, queue_timeout_s=0.05)
    await bh.acquire()
    with pytest.raises(SaturatedError):
        await bh.acquire()
    bh.release()
    await bh.acquire()
    assert bh.in_flight == 1
