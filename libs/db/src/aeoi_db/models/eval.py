"""eval schema - owner: evaluation. Versioned datasets, runs and per-case metrics."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import EMPTY_JSON, Base, created_at_col, in_list, uuid_pk

SCHEMA = "eval"
RUN_STATUSES = ("RUNNING", "SUCCEEDED", "FAILED")


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("name", "version"), {"schema": SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class EvalCase(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (UniqueConstraint("dataset_id", "case_key"), {"schema": SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.datasets.id", ondelete="CASCADE"), nullable=False
    )
    case_key: Mapped[str] = mapped_column(String(80), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint(in_list("status", RUN_STATUSES), name="status_valid"),
        # serves: "runs of dataset X, newest first" (A/B comparison picker)
        Index("ix_eval_runs_dataset_id_started_at", "dataset_id", text("started_at DESC")),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.datasets.id", ondelete="RESTRICT"), nullable=False
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # model, prompts, k...
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(40))
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="RUNNING")
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    started_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evaluation(Base):
    """One metric value for one case in one run (long format: easy to add metrics)."""

    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", "metric"),  # also serves per-run reads
        # serves: FK lookup + "how did case X score across runs"
        Index("ix_evaluations_case_id", "case_id"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.eval_cases.id", ondelete="RESTRICT"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(60), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
