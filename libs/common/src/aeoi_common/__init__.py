"""Shared building blocks for every AEOI Python service."""

from aeoi_common.correlation import get_correlation_id, new_correlation_id, set_correlation_id
from aeoi_common.errors import (
    AEOIError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PreconditionFailedError,
    PreconditionRequiredError,
    ProblemDetail,
    RateLimitedError,
    UnauthorizedError,
    UpstreamUnavailableError,
    ValidationFailedError,
)
from aeoi_common.ids import uuid7
from aeoi_common.settings import BaseServiceSettings, Environment

__all__ = [
    "AEOIError",
    "BadRequestError",
    "BaseServiceSettings",
    "ConflictError",
    "Environment",
    "ForbiddenError",
    "NotFoundError",
    "PreconditionFailedError",
    "PreconditionRequiredError",
    "ProblemDetail",
    "RateLimitedError",
    "UnauthorizedError",
    "UpstreamUnavailableError",
    "ValidationFailedError",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
    "uuid7",
]
