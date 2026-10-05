"""llm schema - owner: llm-gateway. Every model call: tokens, latency, cost, versions."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import Base, created_at_col, in_list, uuid_pk

SCHEMA = "llm"
USAGE_STATUSES = ("OK", "ERROR", "TIMEOUT", "RATE_LIMITED", "FALLBACK")
OPERATIONS = ("generate", "generate_structured", "stream", "embed")


class ModelUsage(Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        CheckConstraint(in_list("status", USAGE_STATUSES), name="status_valid"),
        CheckConstraint(in_list("operation", OPERATIONS), name="operation_valid"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0",
            name="tokens_non_negative",
        ),
        # serves: cost/latency dashboards by model over time
        Index("ix_model_usage_model_created_at", "model", text("created_at DESC")),
        # serves: cost per investigation (KPI)
        Index("ix_model_usage_investigation_id", "investigation_id"),
        # serves: time-window rollups across all models; BRIN fits append-only data
        Index("ix_model_usage_created_at_brin", "created_at", postgresql_using="brin"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)  # idempotent
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    prompt_id: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    agent_name: Mapped[str | None] = mapped_column(String(64))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(14), nullable=False)
    error_type: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class PromptVersion(Base):
    """Registry mirror of prompts/<agent>/vN.md (git is the source; this is for joins)."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="sha_hex"),
        {"schema": SCHEMA},
    )
    prompt_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
