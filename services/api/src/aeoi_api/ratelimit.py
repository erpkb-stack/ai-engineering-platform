"""Token-bucket rate limiter, keyed by user.

[P] in-process (correct for ONE replica). [Prod] Redis + Lua so all replicas share the
budget (Phase 19). The interface stays the same, so routes don't change.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol


class RateLimiter(Protocol):
    async def acquire(self, key: str) -> float:
        """0.0 if allowed, else seconds until a token is available."""
        ...


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class InMemoryTokenBucket:
    rate_per_s: float
    burst: int
    clock: object = field(default=time.monotonic)
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    max_keys: int = 50_000  # bound memory: evict the oldest bucket when full

    async def acquire(self, key: str) -> float:
        now = self.clock()  # type: ignore[operator]
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    oldest = min(self._buckets, key=lambda k: self._buckets[k].updated)
                    del self._buckets[oldest]
                bucket = self._buckets[key] = _Bucket(tokens=float(self.burst), updated=now)
            bucket.tokens = min(
                self.burst, bucket.tokens + (now - bucket.updated) * self.rate_per_s
            )
            bucket.updated = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return 0.0
            return (1 - bucket.tokens) / self.rate_per_s
