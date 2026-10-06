from __future__ import annotations

import asyncio
import random

import pytest

from aeoi_llm.providers.base import PermanentError, RateLimitedError, RetryableError
from aeoi_llm.resilience import (
    BreakerState,
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    SaturatedError,
    with_retries,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def failing(errors: list[Exception], result: str = "ok"):
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return result

    return fn, calls


async def test_retries_retryable_then_succeeds() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    fn, calls = failing([RetryableError("a"), RetryableError("b")])
    assert (
        await with_retries(fn, RetryPolicy(max_attempts=3), sleep=sleep, rng=random.Random(1))
        == "ok"
    )
    assert calls["n"] == 3
    assert len(sleeps) == 2


async def test_permanent_error_is_not_retried() -> None:
    fn, calls = failing([PermanentError("bad request", status=400)])
    with pytest.raises(PermanentError):
        await with_retries(fn, RetryPolicy(max_attempts=5))
    assert calls["n"] == 1


async def test_gives_up_after_max_attempts() -> None:
    async def sleep(_: float) -> None:
        return None

    fn, calls = failing([RetryableError("x")] * 5)
    with pytest.raises(RetryableError):
        await with_retries(fn, RetryPolicy(max_attempts=3), sleep=sleep)
    assert calls["n"] == 3


async def test_honours_short_retry_after() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    fn, _ = failing([RateLimitedError("slow down", retry_after_s=3)])
    await with_retries(fn, RetryPolicy(base_delay_s=0.01), sleep=sleep)
    assert sleeps == [3]


async def test_long_retry_after_fails_fast_so_router_can_fall_back() -> None:
    fn, calls = failing([RateLimitedError("slow down", retry_after_s=60)])
    with pytest.raises(RateLimitedError):
        await with_retries(fn, RetryPolicy(max_retry_after_s=10))
    assert calls["n"] == 1


def test_full_jitter_is_bounded() -> None:
    p = RetryPolicy(base_delay_s=0.5, max_delay_s=8)
    rng = random.Random(7)
    for attempt in range(1, 10):
        assert 0 <= p.delay(attempt, rng) <= 8


async def test_breaker_opens_then_half_opens_then_closes() -> None:
    clock = Clock()
    br = CircuitBreaker("p", threshold=2, cooldown_s=30, clock=clock)

    async def boom() -> str:
        raise RetryableError("down")

    async def fine() -> str:
        return "ok"

    for _ in range(2):
        with pytest.raises(RetryableError):
            await br.call(boom)
    assert br.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await br.call(fine)  # fast fail, provider not called

    clock.t = 31
    assert br.state is BreakerState.HALF_OPEN
    assert await br.call(fine) == "ok"
    assert br.state is BreakerState.CLOSED


async def test_failed_trial_reopens_breaker() -> None:
    clock = Clock()
    br = CircuitBreaker("p", threshold=1, cooldown_s=10, clock=clock)

    async def boom() -> str:
        raise RetryableError("down")

    with pytest.raises(RetryableError):
        await br.call(boom)
    clock.t = 11
    with pytest.raises(RetryableError):
        await br.call(boom)  # the trial
    assert br.state is BreakerState.OPEN


async def test_permanent_errors_do_not_open_breaker() -> None:
    br = CircuitBreaker("p", threshold=1)

    async def bad() -> str:
        raise PermanentError("400", status=400)

    for _ in range(3):
        with pytest.raises(PermanentError):
            await br.call(bad)
    assert br.state is BreakerState.CLOSED


async def test_bulkhead_rejects_when_full() -> None:
    bh = Bulkhead("ollama", limit=1, queue_timeout_s=0.05)
    await bh.acquire()
    with pytest.raises(SaturatedError):
        await bh.acquire()
    bh.release()
    await asyncio.wait_for(bh.acquire(), 1)
    assert bh.in_flight == 1
