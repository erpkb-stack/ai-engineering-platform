"""devdata schema - owner: tool-gateway.

SIMULATED external systems (CD system, Git host, log store, metrics store) behind the
Tool Gateway. In production these are real APIs (Argo/Spinnaker, GitHub, Loki/Splunk,
Prometheus); only the tool adapters change. Rows reference services by `service_key`
(the catalog slug), never by FK - the catalog is a different service (Phase 20).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import EMPTY_JSON, EMPTY_TEXT_ARRAY, Base, in_list, uuid_pk

SCHEMA = "devdata"
ENVIRONMENTS = ("development", "staging", "production")
DEPLOY_STATUSES = ("STARTED", "SUCCEEDED", "FAILED", "ROLLED_BACK")
PR_STATES = ("OPEN", "MERGED", "CLOSED")
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(in_list("environment", ENVIRONMENTS), name="environment_valid"),
        CheckConstraint(in_list("status", DEPLOY_STATUSES), name="status_valid"),
        CheckConstraint("deploy_key ~ '^DEPLOY-[0-9]+$'", name="key_format"),
        # serves: get_deployment(service, before T) - "recent deploys of X, newest first"
        Index(
            "ix_deployments_service_key_environment_started_at",
            "service_key",
            "environment",
            text("started_at DESC"),
        ),
        # serves: correlate deploys in a time window across all services (incident triage)
        Index("ix_deployments_started_at", "started_at", postgresql_using="brin"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    deploy_key: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    service_key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. 2026.10.02.4
    environment: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    deployed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    config_diff: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Commit(Base):
    __tablename__ = "commits"
    __table_args__ = (
        CheckConstraint("sha ~ '^[0-9a-f]{40}$'", name="sha_hex"),
        # serves: get_commit history for a repository, newest first
        Index("ix_commits_repository_committed_at", "repository", text("committed_at DESC")),
        # serves: "what changed in service X between two deploys"
        Index("ix_commits_service_key_committed_at", "service_key", text("committed_at DESC")),
        {"schema": SCHEMA},
    )
    sha: Mapped[str] = mapped_column(String(40), primary_key=True)
    repository: Mapped[str] = mapped_column(String(120), nullable=False)
    service_key: Mapped[str] = mapped_column(String(100), nullable=False)
    author: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    files_changed: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_TEXT_ARRAY
    )
    additions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        CheckConstraint(in_list("state", PR_STATES), name="state_valid"),
        CheckConstraint("(state = 'MERGED') = (merged_at IS NOT NULL)", name="merged_has_time"),
        UniqueConstraint("repository", "number"),  # also serves lookup by repo + PR number
        # serves: "PR that produced commit X" (release risk analysis)
        Index("ix_pull_requests_merge_commit_sha", "merge_commit_sha"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    repository: Mapped[str] = mapped_column(String(120), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    author: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    changed_files: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    merge_commit_sha: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LogEvent(Base):
    """Append-only, time-ordered. [Prod]: daily partitions + retention, or a real log store."""

    __tablename__ = "log_events"
    __table_args__ = (
        CheckConstraint(in_list("level", LOG_LEVELS), name="level_valid"),
        # serves: search_logs(service, time range) - the main access path
        Index("ix_log_events_service_key_ts", "service_key", "ts"),
        # serves: error-only scans (most investigation queries) - partial = much smaller
        Index(
            "ix_log_events_errors",
            "service_key",
            "ts",
            "error_code",
            postgresql_where=text("level IN ('ERROR','FATAL')"),
        ),
        # serves: cross-service time-window scans; BRIN is tiny on append-only time data
        Index("ix_log_events_ts_brin", "ts", postgresql_using="brin"),
        {"schema": SCHEMA},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_key: Mapped[str] = mapped_column(String(100), nullable=False)
    level: Mapped[str] = mapped_column(String(5), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )


class MetricPoint(Base):
    __tablename__ = "metric_points"
    # PK (service_key, metric, ts) serves query_metrics(service, metric, range) directly.
    __table_args__ = {"schema": SCHEMA}
    service_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    metric: Mapped[str] = mapped_column(String(100), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    value: Mapped[float] = mapped_column(Double, nullable=False)
