"""Structured JSON logging: service, env and correlation_id on every line; secrets redacted.

Use:  configure_logging("incident-service", level="INFO"); log = get_logger(__name__)
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from aeoi_common.correlation import get_correlation_id
from aeoi_security.redaction import redact_mapping


def _add_correlation_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    cid = get_correlation_id()
    if cid and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return redact_mapping(dict(event_dict))


def configure_logging(
    service_name: str,
    *,
    level: str = "INFO",
    json_output: bool = True,
    environment: str = "development",
) -> None:
    """Configure structlog + stdlib logging once at process start."""
    log_level = logging.getLevelNamesMapping().get(level.upper())
    if log_level is None:
        raise ValueError(f"unknown log level: {level}")

    # Exceptions: JSON gets a dict traceback; the console renderer formats exc_info itself.
    # (A dict traceback fed to ConsoleRenderer crashes it - and a crash inside an `except`
    # block turns a handled error into a 500. Found in Phase 7.) Never show frame locals:
    # they can hold tokens, passwords, request bodies (found in Phase 4).
    exceptions: list[Any] = (
        [
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            )
        ]
        if json_output
        else []
    )
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_correlation_id,
            *exceptions,
            _redact,  # LAST before rendering: nothing escapes redaction
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service_name, env=environment)
    # Route stdlib logs (uvicorn, sqlalchemy) to the same level/stream.
    logging.basicConfig(level=log_level, stream=sys.stdout, format="%(message)s", force=True)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
