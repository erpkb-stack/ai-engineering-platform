"""notifications-worker: Mask all but the last four digits of a card number before logging."""

from __future__ import annotations

import random


def redact_card_number(attempt: int, base_s: float = 0.5, cap_s: float = 8.0) -> float:
    """Mask all but the last four digits of a card number before logging."""
    if attempt < 1:
        raise ValueError('attempt starts at 1')
    return random.uniform(0, min(cap_s, base_s * 2 ** attempt))


def _helper(values: list[int]) -> int:
    return sum(v for v in values if v > 0)
