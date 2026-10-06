"""LLM-gateway bindings of the shared resilience primitives (aeoi_common.resilience).

The gateway's fallback logic catches `ProviderError`, so "circuit open" and "saturated" must be
ProviderErrors too: these subclasses make them both a RetryableError (gateway semantics) and a
TransientError (shared retry/breaker semantics).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from aeoi_common import resilience as _r
from aeoi_common.resilience import BreakerState, RetryPolicy, with_retries
from aeoi_llm.providers.base import RetryableError


class CircuitOpenError(RetryableError, _r.CircuitOpenError):
    """Fast failure: the provider/model is known-bad."""


class SaturatedError(RetryableError, _r.SaturatedError):
    """Bulkhead full for this model."""


@dataclass
class CircuitBreaker(_r.CircuitBreaker):
    open_error: ClassVar[type[Exception]] = CircuitOpenError


class Bulkhead(_r.Bulkhead):
    saturated_error: ClassVar[type[Exception]] = SaturatedError


__all__ = [
    "BreakerState",
    "Bulkhead",
    "CircuitBreaker",
    "CircuitOpenError",
    "RetryPolicy",
    "SaturatedError",
    "with_retries",
]
