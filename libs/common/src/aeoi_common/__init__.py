"""Shared building blocks for every AEOI Python service."""

from aeoi_common.correlation import get_correlation_id, new_correlation_id, set_correlation_id
from aeoi_common.errors import (
    AEOIError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ProblemDetail,
    ValidationFailedError,
)
from aeoi_common.ids import uuid7
from aeoi_common.settings import BaseServiceSettings, Environment

__all__ = [
    "AEOIError",
    "BaseServiceSettings",
    "ConflictError",
    "Environment",
    "ForbiddenError",
    "NotFoundError",
    "ProblemDetail",
    "ValidationFailedError",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
    "uuid7",
]
