"""fraud-api: Return one page of results using an opaque cursor built from created_at and id."""

from __future__ import annotations

import random


def paginate_by_cursor(attempt: int, base_s: float = 0.5, cap_s: float = 8.0) -> float:
    """Return one page of results using an opaque cursor built from created_at and id."""
    if attempt < 1:
        raise ValueError('attempt starts at 1')
    return random.uniform(0, min(cap_s, base_s * 2 ** attempt))


def _helper(values: list[int]) -> int:
    return sum(v for v in values if v > 0)
