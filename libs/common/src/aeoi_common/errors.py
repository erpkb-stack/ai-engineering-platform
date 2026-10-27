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


class PayloadTooLargeError(AEOIError):
    status = 413
    type_slug = "payload-too-large"
    title = "Payload too large"


# Phase 28a (ADR-040): driver/ORM classes that mean "could not talk to the database"
# (refused, dropped, server shutting down - and their subclasses, e.g. psycopg's AdminShutdown).
# Matched by module + name along the MRO, so this lib imports neither SQLAlchemy nor psycopg.
_DB_DOWN = {("sqlalchemy.exc", "OperationalError"), ("sqlalchemy.exc", "InterfaceError"),
            ("psycopg", "OperationalError"), ("psycopg", "InterfaceError")}  # fmt: skip


def database_unavailable(exc: BaseException) -> str | None:
    """The class name if `exc` (or its cause/context chain) is a lost database connection."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if any((c.__module__, c.__name__) in _DB_DOWN for c in type(e).__mro__):
            return type(e).__name__
        e = e.__cause__ or e.__context__
    return None
