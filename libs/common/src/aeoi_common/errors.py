"""Domain errors that map 1:1 to RFC 9457 (formerly 7807) problem+json responses."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aeoi_common.correlation import get_correlation_id


class ProblemDetail(BaseModel):
    """RFC 9457 problem details, plus our correlation id."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(default="about:blank")
    title: str
    status: int = Field(ge=400, le=599)
    detail: str | None = None
    instance: str | None = None
    correlation_id: str | None = None
    errors: list[dict[str, Any]] | None = None


class AEOIError(Exception):
    """Base class. Subclasses set status and a stable problem type slug."""

    status: ClassVar[int] = 500
    type_slug: ClassVar[str] = "internal-error"
    title: ClassVar[str] = "Internal error"

    def __init__(self, detail: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors

    def to_problem(self, instance: str | None = None) -> ProblemDetail:
        return ProblemDetail(
            type=f"https://aeoi.example/problems/{self.type_slug}",
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            correlation_id=get_correlation_id(),
            errors=self.errors,
        )


class NotFoundError(AEOIError):
    status = 404
    type_slug = "not-found"
    title = "Resource not found"


class ConflictError(AEOIError):
    status = 409
    type_slug = "conflict"
    title = "Conflict"


class ForbiddenError(AEOIError):
    status = 403
    type_slug = "forbidden"
    title = "Forbidden"


class ValidationFailedError(AEOIError):
    status = 422
    type_slug = "validation-failed"
    title = "Validation failed"


class UnauthorizedError(AEOIError):
    status = 401
    type_slug = "unauthorized"
    title = "Authentication required"


class PreconditionFailedError(AEOIError):
    status = 412
    type_slug = "precondition-failed"
    title = "Precondition failed"


class PreconditionRequiredError(AEOIError):
    status = 428
    type_slug = "precondition-required"
    title = "Precondition required"


class RateLimitedError(AEOIError):
    status = 429
    type_slug = "rate-limited"
    title = "Too many requests"


class UpstreamUnavailableError(AEOIError):
    status = 503
    type_slug = "upstream-unavailable"
    title = "Dependency unavailable"


class BadRequestError(AEOIError):
    status = 400
    type_slug = "bad-request"
    title = "Bad request"
