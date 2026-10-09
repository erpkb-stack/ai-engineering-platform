"""Shared FastAPI building blocks for AEOI services."""

from aeoi_web.app import create_app
from aeoi_web.auth import (
    Authenticator,
    CurrentPrincipal,
    current_principal,
    require,
    require_acting_user,
    require_scope,
)
from aeoi_web.health import ReadinessCheck, health_router
from aeoi_web.problems import PROBLEM_JSON, problem_response

__all__ = [
    "PROBLEM_JSON",
    "Authenticator",
    "CurrentPrincipal",
    "ReadinessCheck",
    "create_app",
    "current_principal",
    "health_router",
    "problem_response",
    "require",
    "require_acting_user",
    "require_scope",
]
