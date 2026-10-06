"""cart-api: Return the delay before the next retry: exponential growth with full jitter, capped."""

from __future__ import annotations

import random


def compute_backoff_with_jitter(attempt: int, base_s: float = 0.5, cap_s: float = 8.0) -> float:
    """Return the delay before the next retry: exponential growth with full jitter, capped."""
    if attempt < 1:
        raise ValueError('attempt starts at 1')
    return random.uniform(0, min(cap_s, base_s * 2 ** attempt))


def _helper(values: list[int]) -> int:
    return sum(v for v in values if v > 0)
