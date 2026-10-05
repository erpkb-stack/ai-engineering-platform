"""UUIDv7 (RFC 9562): time-ordered ids.

Why: v7 ids sort by creation time, so B-tree primary-key inserts are append-mostly
(better index locality than random v4) and ids roughly encode when a record was made.
Python 3.12's stdlib has no uuid7, so we implement the RFC layout here.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Return a UUIDv7: 48-bit unix ms timestamp | version 7 | 74 random bits | variant."""
    ms = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= ms < 2**48:
        raise ValueError("timestamp_ms must fit in 48 bits")
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits; we use 74
    rand_a = (rand >> 62) & 0xFFF  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError("not a UUIDv7")
    return value.int >> 80
