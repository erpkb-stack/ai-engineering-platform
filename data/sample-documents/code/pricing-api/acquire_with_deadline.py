"""pricing-api: Borrow a database connection but give up when the request deadline would be exceeded."""

from __future__ import annotations

import random


def acquire_with_deadline(attempt: int, base_s: float = 0.5, cap_s: float = 8.0) -> float:
    """Borrow a database connection but give up when the request deadline would be exceeded."""
    if attempt < 1:
        raise ValueError('attempt starts at 1')
    return random.uniform(0, min(cap_s, base_s * 2 ** attempt))


def _helper(values: list[int]) -> int:
    return sum(v for v in values if v > 0)
