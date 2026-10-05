"""Incident API contracts (Feature 19)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


class Severity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


ServiceKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")]


def _no_dupes(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("duplicate service keys")
    return values


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(
        min_length=5, max_length=300, examples=["HTTP 500 errors increased 42% after deployment"]
    )
    description: str = Field(default="", max_length=20_000)
    severity: Severity
    affected_services: Annotated[list[ServiceKey], AfterValidator(_no_dupes)] = Field(
        default_factory=list, max_length=50
    )
    detected_at: datetime | None = Field(default=None, description="defaults to now (UTC)")


class IncidentPatch(BaseModel):
    """Partial update. Send `If-Match: <version>` (optimistic locking)."""

    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus | None = None
    severity: Severity | None = None
    reason: str = Field(min_length=3, max_length=2000, description="recorded in the timeline")


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str = Field(examples=["INC-10234"])
    title: str
    description: str
    severity: Severity
    status: IncidentStatus
    affected_services: list[str]
    detected_at: datetime
    resolved_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int


class TimelineEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    occurred_at: datetime
    source: str
    event_type: str
    summary: str
    actor: str
    evidence_key: str | None


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    evidence_key: str
    kind: str
    source_system: str
    title: str
    excerpt: str
    observed_at: datetime | None
    retrieved_by: str
    uri: str | None


class InvestigationAccepted(BaseModel):
    """202 body: the investigation runs asynchronously (orchestrator, Phase 9)."""

    request_id: UUID
    incident_id: UUID
    status: str = "REQUESTED"


class FeedbackTarget(StrEnum):
    FINDING = "FINDING"
    REPORT = "REPORT"
    ANSWER = "ANSWER"
    RETRIEVAL = "RETRIEVAL"


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID | None = None
    target_type: FeedbackTarget
    target_id: str = Field(min_length=1, max_length=120)
    rating: Literal[-1, 1] = Field(description="1 = helpful, -1 = not helpful")
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    id: UUID
    created_at: datetime


# ---- event payloads (published via the outbox) ----
class IncidentCreatedPayload(BaseModel):
    incident_id: UUID
    key: str
    title: str
    severity: Severity
    affected_services: list[str]
    detected_at: datetime


class IncidentUpdatedPayload(BaseModel):
    incident_id: UUID
    key: str
    changes: dict[str, list[str]]  # field -> [old, new]
    reason: str
    version: int


class InvestigationRequestedPayload(BaseModel):
    request_id: UUID
    incident_id: UUID
    key: str
    requested_by: str
