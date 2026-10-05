"""orchestrator schema - owner: orchestrator. Investigations, agent tasks and executions.

LangGraph's own checkpoint tables are created by its PostgresSaver (Phase 9), not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import EMPTY_JSON, Base, created_at_col, in_list, uuid_pk

SCHEMA = "orchestrator"
INVESTIGATION_STATUSES = (
    "RUNNING",
    "AWAITING_REVIEW",
    "COMPLETE",
    "INCONCLUSIVE",
    "FAILED",
    "CANCELLED",
)
TASK_STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT")
MESSAGE_ROLES = ("system", "user", "assistant", "tool")


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint(in_list("status", INVESTIGATION_STATUSES), name="status_valid"),
        CheckConstraint("budget_usd > 0 AND spent_usd >= 0", name="budget_non_negative"),
        # serves: "investigations for incident X, latest first" (incident page)
        Index("ix_investigations_incident_id_started_at", "incident_id", text("started_at DESC")),
        # one RUNNING investigation per incident (no duplicate work on redelivery)
        Index(
            "uq_investigations_one_running",
            "incident_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # soft ref
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RUNNING")
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSON)
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default="0")
    started_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base):
    """One unit of agent work (an AgentTask event). Idempotent on idempotency_key."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(in_list("status", TASK_STATUSES), name="status_valid"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        # serves: fan-in "all tasks of investigation X"
        Index("ix_tasks_investigation_id", "investigation_id"),
        # serves: reaper "find stuck tasks past their deadline" - partial: only live tasks
        Index(
            "ix_tasks_live_deadline",
            "deadline_at",
            postgresql_where=text("status IN ('PENDING','RUNNING')"),
        ),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.investigations.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="PENDING")
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSON)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class AgentExecution(Base):
    """What an agent actually did: model, prompt version, tokens, latency, cost (Feature 12)."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        CheckConstraint(in_list("status", TASK_STATUSES), name="status_valid"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_non_negative"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="time_order"),
        # serves: agent trace view for a task
        Index("ix_agent_executions_task_id", "task_id"),
        # serves: per-agent latency/cost dashboards over time
        Index("ix_agent_executions_agent_name_started_at", "agent_name", text("started_at DESC")),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))  # null for deterministic nodes
    prompt_id: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSON)
    started_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    """Scrubbed LLM conversation turns of an execution (debugging/replay; NOT in traces)."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(in_list("role", MESSAGE_ROLES), name="role_valid"),
        UniqueConstraint("execution_id", "seq"),  # also serves ordered reads per execution
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agent_executions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()
