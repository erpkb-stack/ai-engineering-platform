"""Event envelope used on the in-process bus (Phases 4-17) and Kafka (Phase 18+).

Same shape on both transports, so swapping the bus is not a rewrite.
Delivery is at-least-once: consumers MUST dedupe on `event_id`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aeoi_common.correlation import get_correlation_id
from aeoi_common.ids import uuid7


class EventType(StrEnum):
    INCIDENT_CREATED = "IncidentCreated"
    INVESTIGATION_STARTED = "InvestigationStarted"
    AGENT_STARTED = "AgentStarted"
    AGENT_COMPLETED = "AgentCompleted"
    AGENT_FAILED = "AgentFailed"
    EVIDENCE_RETRIEVED = "EvidenceRetrieved"
    HYPOTHESIS_CREATED = "HypothesisCreated"
    HYPOTHESIS_CHALLENGED = "HypothesisChallenged"
    VALIDATION_COMPLETED = "ValidationCompleted"
    HUMAN_REVIEW_REQUIRED = "HumanReviewRequired"
    HUMAN_APPROVED = "HumanApproved"
    HUMAN_REJECTED = "HumanRejected"
    INCIDENT_RESOLVED = "IncidentResolved"


def _now() -> datetime:
    return datetime.now(UTC)


class EventEnvelope[PayloadT: BaseModel](BaseModel):
    """Envelope for every domain event. `payload` is a typed Pydantic model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid7)
    event_type: EventType
    schema_version: int = Field(default=1, ge=1)
    occurred_at: datetime = Field(default_factory=_now)
    correlation_id: str | None = Field(default_factory=get_correlation_id)
    causation_id: UUID | None = None  # event_id of the event that caused this one
    incident_id: UUID | None = None  # Kafka partition key when present
    actor: str  # "user:<id>" | "service:<name>" | "agent:<name>"
    traceparent: str | None = None  # W3C trace context
    payload: PayloadT

    @field_validator("occurred_at")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        return value

    @field_validator("actor")
    @classmethod
    def _actor_has_kind(cls, value: str) -> str:
        kind, _, ident = value.partition(":")
        if kind not in {"user", "service", "agent"} or not ident:
            raise ValueError("actor must look like 'user:<id>', 'service:<name>' or 'agent:<name>'")
        return value

    def partition_key(self) -> bytes | None:
        """Kafka key: per-incident ordering. None = round-robin."""
        return str(self.incident_id).encode() if self.incident_id else None
