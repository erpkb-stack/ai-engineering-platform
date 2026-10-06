"""Audit service contract. Producers (tool-gateway relay; later every service via Kafka,
Phase 18) POST batches; the Security/Audit UI reads with keyset pagination."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_DETAILS_BYTES = 16_384


class AuditEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(description="producer-assigned; the idempotency key")
    occurred_at: datetime
    actor: str = Field(max_length=160, pattern=r"^(user|service|agent):.+$")
    actor_role: str | None = Field(default=None, max_length=40)
    action: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.]*$")
    resource_type: str = Field(min_length=1, max_length=60)
    resource_id: str | None = Field(default=None, max_length=160)
    incident_id: UUID | None = None
    correlation_id: str | None = Field(default=None, max_length=80)
    outcome: Literal["SUCCESS", "DENIED", "FAILURE"]
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        if len(json.dumps(self.details, default=str).encode()) > MAX_DETAILS_BYTES:
            raise ValueError(f"details larger than {MAX_DETAILS_BYTES} bytes")
        return self


class AuditBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[AuditEventIn] = Field(min_length=1, max_length=500)


class AuditIngestResult(BaseModel):
    received: int
    inserted: int  # received - inserted = duplicates (redelivery), which is fine


class AuditEventOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor: str
    actor_role: str | None
    action: str
    resource_type: str
    resource_id: str | None
    incident_id: UUID | None
    correlation_id: str | None
    outcome: str
    details: dict[str, Any]


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    next_cursor: str | None = None
    scope: Literal["all", "own"]  # "incident"/"team" need assignment data (Phase 16/26)
