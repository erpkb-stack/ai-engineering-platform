from aeoi_api.ratelimit import InMemoryTokenBucket


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def test_bucket_refills_over_time() -> None:
    clock = Clock()
    bucket = InMemoryTokenBucket(rate_per_s=1.0, burst=2, clock=clock)
    assert await bucket.acquire("u") == 0
    assert await bucket.acquire("u") == 0
    assert await bucket.acquire("u") > 0
    clock.t = 1.0
    assert await bucket.acquire("u") == 0


async def test_users_are_independent() -> None:
    bucket = InMemoryTokenBucket(rate_per_s=0.001, burst=1, clock=Clock())
    assert await bucket.acquire("a") == 0
    assert await bucket.acquire("b") == 0
    assert await bucket.acquire("a") > 0


async def test_memory_is_bounded() -> None:
    bucket = InMemoryTokenBucket(rate_per_s=1, burst=1, clock=Clock(), max_keys=3)
    for user in "abcd":
        await bucket.acquire(user)
    assert len(bucket._buckets) == 3
