"""tools schema - owner: tool-gateway. One row per tool call (allowed or denied)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import EMPTY_JSON, Base, created_at_col, in_list, uuid_pk

SCHEMA = "tools"
SIDE_EFFECTS = ("READ", "WRITE", "CONSEQUENTIAL")
CALL_STATUSES = ("OK", "DENIED", "ERROR", "TIMEOUT")


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(in_list("side_effect", SIDE_EFFECTS), name="side_effect_valid"),
        CheckConstraint(in_list("status", CALL_STATUSES), name="status_valid"),
        # Defence in depth for ADR-007: the DB itself refuses to record an executed
        # consequential call that has no approval.
        CheckConstraint(
            "side_effect <> 'CONSEQUENTIAL' OR status = 'DENIED' OR approval_id IS NOT NULL",
            name="consequential_needs_approval",
        ),
        CheckConstraint(
            "status <> 'DENIED' OR denial_reason IS NOT NULL", name="denial_has_reason"
        ),
        # serves: agent trace / evidence view per incident in time order
        Index("ix_tool_calls_incident_id_created_at", "incident_id", "created_at"),
        # serves: per-tool success/latency dashboards
        Index("ix_tool_calls_tool_name_created_at", "tool_name", text("created_at DESC")),
        # serves: security dashboard "recent denials" - partial, denials are rare
        Index("ix_tool_calls_denied", "created_at", postgresql_where=text("status = 'DENIED'")),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_name: Mapped[str | None] = mapped_column(String(64))
    on_behalf_of: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    side_effect: Mapped[str] = mapped_column(String(14), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # redacted
    output_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    denial_reason: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = created_at_col()
