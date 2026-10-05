"""audit schema - owner: audit. Append-only, partitioned by month.

Append-only is enforced twice: (1) the svc_audit role has only INSERT/SELECT, and
(2) a trigger rejects UPDATE/DELETE even for the table owner. Retention = DROP PARTITION
(instant, no bloat) instead of DELETE.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_common.ids import uuid7
from aeoi_db.base import EMPTY_JSON, Base, in_list

SCHEMA = "audit"
OUTCOMES = ("SUCCESS", "DENIED", "FAILURE")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        # Partition key must be part of the PK in Postgres.
        PrimaryKeyConstraint("id", "occurred_at"),
        CheckConstraint(in_list("outcome", OUTCOMES), name="outcome_valid"),
        CheckConstraint("actor ~ '^(user|service|agent):.+$'", name="actor_format"),
        # serves: "everything actor X did" (security investigations), newest first
        Index("ix_audit_events_actor_occurred_at", "actor", text("occurred_at DESC")),
        # serves: audit trail tab of an incident
        Index("ix_audit_events_incident_id_occurred_at", "incident_id", "occurred_at"),
        # serves: follow one request end-to-end across services
        Index("ix_audit_events_correlation_id", "correlation_id"),
        {"schema": SCHEMA, "postgresql_partition_by": "RANGE (occurred_at)"},
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid7)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(
        String(80), nullable=False
    )  # e.g. tool.call, approval.decide
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(160))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[str | None] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
