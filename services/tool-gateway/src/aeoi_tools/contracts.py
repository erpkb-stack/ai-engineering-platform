"""Tool contract (architecture.md §14, `/new-tool` skill). A tool without ALL fields is refused
at import time by `ToolSpec.__post_init__` and by the contract-lint test."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from aeoi_common.resilience import TransientError
from aeoi_models.findings import EvidenceKind
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm


class SideEffect(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    CONSEQUENTIAL = "CONSEQUENTIAL"


@dataclass(frozen=True)
class ToolContext:
    """Who is calling, for whom, in what investigation. Built from VERIFIED tokens only."""

    user: Principal  # the human the call is made for (always a user, never a service)
    user_token: str  # forwarded to downstream services that do their own authz (rag)
    agent: str | None  # None = the user is calling directly (manual mode)
    service: str | None  # verified service subject when an agent calls, e.g. service:agents
    incident_id: UUID | None = None
    investigation_id: UUID | None = None
    task_id: UUID | None = None
    approval_id: UUID | None = None
    correlation_id: str | None = None

    @property
    def actor(self) -> str:
        return f"agent:{self.agent}" if self.agent else f"user:{self.user.user_id}"


Handler = Callable[[Any, ToolContext], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str  # written for the LLM: what it returns AND when not to use it
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permissions: frozenset[Perm]
    side_effect: SideEffect
    handler: Handler
    dependency: str  # breaker/bulkhead key: one sick backend trips all tools that use it
    # evidence ids are "<KIND>-<tool_call_id hex>-<n>" (aeoi_models.findings.EvidenceKind), so a
    # finding can cite them directly and incident.evidence accepts them (key LIKE kind || '-%')
    evidence_kind: str
    timeout_s: float = 5.0
    max_attempts: int = 2  # retries only for idempotent READs (enforced below)
    idempotent: bool = True
    max_items: int = 200  # output cap (items lists are truncated, flagged)
    max_output_bytes: int = 256_000
    audit_fields: tuple[str, ...] = ()  # input fields copied into the audit event
    tags: tuple[str, ...] = field(default=())
    # per-item kind when one tool returns mixed kinds (search_repository: commits + PRs)
    item_kind: Callable[[dict[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if not self.name.replace("_", "").isalnum() or "_" not in self.name:
            raise ValueError(f"tool name must be snake_case verb_noun: {self.name}")
        if len(self.description) < 40:
            raise ValueError(f"{self.name}: description too short to guide a model")
        if self.evidence_kind not in {k.value for k in EvidenceKind}:
            raise ValueError(f"{self.name}: unknown evidence_kind {self.evidence_kind!r}")
        if not self.dependency:
            raise ValueError(f"{self.name}: dependency is required")
        if not self.permissions:
            raise ValueError(f"{self.name}: a tool must require at least one permission")
        if self.side_effect is not SideEffect.READ and self.max_attempts > 1:
            raise ValueError(f"{self.name}: only READ tools may retry")
        missing = set(self.audit_fields) - set(self.input_model.model_fields)
        if missing:
            raise ValueError(f"{self.name}: audit_fields not in input: {missing}")


class ToolUnavailableError(TransientError):
    """Backend temporarily unavailable (timeout, 5xx, connection). Retryable for READs."""


class ToolExecutionError(Exception):
    """Backend refused or failed permanently (ambiguous reference, upstream 4xx). Not retried.
    The message is shown to the caller, so it must not contain data the caller can't see."""

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class EgressBlockedError(Exception):
    """A tool tried to reach a host outside the egress allow-list."""


class ToolNotFoundError(Exception):
    pass
