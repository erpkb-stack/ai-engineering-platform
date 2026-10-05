"""incident schema - owner: incident-service.

Incidents, their timeline, the evidence graph (evidence <-> hypotheses), human approvals,
feedback, and the transactional outbox / consumer dedupe tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import (
    EMPTY_JSON,
    EMPTY_TEXT_ARRAY,
    Base,
    created_at_col,
    in_list,
    updated_at_col,
    uuid_pk,
)

SCHEMA = "incident"
SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")
INCIDENT_STATUSES = ("OPEN", "INVESTIGATING", "AWAITING_REVIEW", "MITIGATED", "RESOLVED", "CLOSED")
EVIDENCE_KINDS = (
    "LOG",
    "METRIC",
    "TRACE",
    "DEPLOY",
    "CONFIG",
    "COMMIT",
    "PR",
    "DOC",
    "RUNBOOK",
    "INCIDENT",
    "ALERT",
)
CONFIDENCE = ("LOW", "MEDIUM", "HIGH")
HYPOTHESIS_STATUSES = ("PROPOSED", "CHALLENGED", "VALIDATED", "REJECTED", "CONFIRMED")
STANCES = ("SUPPORTS", "CONTRADICTS")
DECISIONS = ("APPROVED", "REJECTED", "MORE_INFO")
EVENT_SOURCES = ("DEPLOY", "CONFIG", "ALERT", "LOG", "METRIC", "TRACE", "HUMAN", "AGENT", "SYSTEM")

incident_number_seq = Sequence("incident_number_seq", schema=SCHEMA, start=10000)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(in_list("severity", SEVERITIES), name="severity_valid"),
        CheckConstraint(in_list("status", INCIDENT_STATUSES), name="status_valid"),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= detected_at", name="resolved_after_detected"
        ),
        CheckConstraint(
            "status NOT IN ('RESOLVED','CLOSED') OR resolved_at IS NOT NULL",
            name="resolved_has_timestamp",
        ),
        # serves: dashboard "open incidents by severity, newest first"
        Index(
            "ix_incidents_status_severity_created_at", "status", "severity", text("created_at DESC")
        ),
        # serves: "incidents affecting service X" (array containment @>)
        Index("ix_incidents_affected_services", "affected_services", postgresql_using="gin"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[int] = mapped_column(
        BigInteger,
        incident_number_seq,
        server_default=incident_number_seq.next_value(),
        nullable=False,
        unique=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="OPEN")
    affected_services: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)), nullable=False, server_default=EMPTY_TEXT_ARRAY
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)  # actor string
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    # Optimistic locking: UPDATE ... WHERE id = :id AND version = :expected
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    @property
    def key(self) -> str:
        return f"INC-{self.number}"


class IncidentEvent(Base):
    """Timeline entries (deploys, alerts, human actions, agent steps)."""

    __tablename__ = "incident_events"
    __table_args__ = (
        CheckConstraint(in_list("source", EVENT_SOURCES), name="source_valid"),
        # serves: timeline reconstruction = range scan per incident in time order
        Index("ix_incident_events_incident_id_occurred_at", "incident_id", "occurred_at"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.incidents.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_key: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = created_at_col()


class Evidence(Base):
    """A piece of evidence retrieved during an investigation. Findings cite evidence_key."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(in_list("kind", EVIDENCE_KINDS), name="kind_valid"),
        CheckConstraint("evidence_key LIKE kind || '-%'", name="key_matches_kind"),
        UniqueConstraint("incident_id", "evidence_key"),
        # serves: evidence explorer filtered by kind within one incident
        Index("ix_evidence_incident_id_kind", "incident_id", "kind"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.incidents.id", ondelete="RESTRICT"), nullable=False
    )
    evidence_key: Mapped[str] = mapped_column(String(80), nullable=False)  # e.g. LOG-18293
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)  # scrubbed, size-capped
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_by: Mapped[str] = mapped_column(String(64), nullable=False)  # agent name
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # tools schema
    uri: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = created_at_col()


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint(in_list("confidence", CONFIDENCE), name="confidence_valid"),
        CheckConstraint(in_list("status", HYPOTHESIS_STATUSES), name="status_valid"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        # serves: ranked hypotheses for an incident (report + UI)
        Index("ix_hypotheses_incident_id_rank", "incident_id", "rank"),
        # serves: FK lookup when following supersession chains
        Index("ix_hypotheses_superseded_by", "superseded_by"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.incidents.id", ondelete="RESTRICT"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="PROPOSED")
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    produced_by: Mapped[str] = mapped_column(String(64), nullable=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.hypotheses.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()


class HypothesisEvidence(Base):
    """Evidence graph edge: hypothesis --SUPPORTS|CONTRADICTS--> evidence."""

    __tablename__ = "hypothesis_evidence"
    __table_args__ = (
        CheckConstraint(in_list("stance", STANCES), name="stance_valid"),
        # serves: reverse edge "which hypotheses use this evidence" (graph view, impact of
        # retracting evidence); the PK already serves hypothesis -> evidence
        Index("ix_hypothesis_evidence_evidence_id", "evidence_id"),
        {"schema": SCHEMA},
    )
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.hypotheses.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.evidence.id", ondelete="RESTRICT"), primary_key=True
    )
    stance: Mapped[str] = mapped_column(String(12), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class Approval(Base):
    """Human decision on a consequential recommendation (Feature 11, ADR-007).

    Single-use (consumed_at), expiring (expires_at), bound to the exact action arguments
    (args_sha256) so an approval for "restart pod A" can never authorise "restart pod B".
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            f"decision IS NULL OR {in_list('decision', DECISIONS)}", name="decision_valid"
        ),
        CheckConstraint("args_sha256 ~ '^[0-9a-f]{64}$'", name="args_sha256_hex"),
        CheckConstraint("expires_at > requested_at", name="expires_after_request"),
        CheckConstraint(
            "(decision IS NULL) = (decided_at IS NULL) AND (decision IS NULL) = (decided_by IS NULL)",
            name="decision_fields_together",
        ),
        CheckConstraint(
            "decision IS NULL OR length(btrim(reason)) >= 3", name="decision_needs_reason"
        ),
        CheckConstraint(
            "consumed_at IS NULL OR decision = 'APPROVED'", name="only_approved_consumed"
        ),
        # serves: approval history per incident (UI + audit)
        Index("ix_approvals_incident_id_requested_at", "incident_id", "requested_at"),
        # at most ONE pending request for the same action on the same incident
        Index(
            "uq_approvals_pending_action",
            "incident_id",
            "args_sha256",
            unique=True,
            postgresql_where=text("decision IS NULL"),
        ),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.incidents.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)  # controlled tool name
    action_args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recommendation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # snapshot
    evidence_keys: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False)
    requested_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(12))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decider_role: Mapped[str | None] = mapped_column(String(40))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(
            in_list("target_type", ("FINDING", "REPORT", "ANSWER", "RETRIEVAL")),
            name="target_type_valid",
        ),
        CheckConstraint("rating IN (-1, 1)", name="rating_thumbs"),
        # serves: feedback for a given finding/report (eval + UI)
        Index("ix_feedback_target_type_target_id", "target_type", "target_id"),
        # serves: FK lookups / feedback per incident
        Index("ix_feedback_incident_id", "incident_id"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.incidents.id", ondelete="SET NULL")
    )
    target_type: Mapped[str] = mapped_column(String(12), nullable=False)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class Outbox(Base):
    """Transactional outbox: written in the SAME transaction as the state change (ADR-003)."""

    __tablename__ = "outbox"
    __table_args__ = (
        # serves: relay polling "next unpublished events in order" - partial, so it only
        # contains the (small) backlog, never the published history
        Index(
            "ix_outbox_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()  # = event_id
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = created_at_col()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ProcessedEvent(Base):
    """Consumer-side dedupe: insert in the same tx as the side effect; conflict = duplicate."""

    __tablename__ = "processed_events"
    __table_args__ = {"schema": SCHEMA}
    consumer: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = created_at_col()
