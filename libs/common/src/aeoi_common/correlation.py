"""Request/investigation correlation id carried in a contextvar.

It flows through async tasks automatically, and services copy it into logs,
outgoing HTTP headers, Kafka headers and audit events.
"""

from __future__ import annotations

from contextvars import ContextVar

from aeoi_common.ids import uuid7

CORRELATION_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str | None] = ContextVar("aeoi_correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def new_correlation_id() -> str:
    """Create, set and return a fresh correlation id."""
    value = str(uuid7())
    _correlation_id.set(value)
    return value
