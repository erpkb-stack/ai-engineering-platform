"""Every error leaves the service as RFC 9457 problem+json with a correlation id."""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aeoi_common.correlation import get_correlation_id
from aeoi_common.errors import AEOIError, ProblemDetail

PROBLEM_JSON = "application/problem+json"
log = structlog.get_logger("aeoi.errors")


def problem_response(problem: ProblemDetail, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(AEOIError)
    async def _aeoi(request: Request, exc: AEOIError) -> JSONResponse:
        headers = {"Retry-After": "1"} if exc.status in (429, 503) else None
        return problem_response(exc.to_problem(instance=request.url.path), headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]  # no "input": it may echo secrets back
        return problem_response(
            ProblemDetail(
                type="https://aeoi.example/problems/validation-failed",
                title="Validation failed",
                status=422,
                detail="Request body or parameters are invalid.",
                instance=request.url.path,
                correlation_id=get_correlation_id(),
                errors=errors,
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            ProblemDetail(
                title=str(exc.detail) if exc.status_code < 500 else "Server error",
                status=exc.status_code,
                instance=request.url.path,
                correlation_id=get_correlation_id(),
            ),
            dict(exc.headers) if exc.headers else None,
        )

    # Unhandled exceptions are turned into a 500 problem by CorrelationIdMiddleware, inside
    # the correlation scope (an Exception handler here would run outside it).
