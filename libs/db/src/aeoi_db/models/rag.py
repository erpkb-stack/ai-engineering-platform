"""rag schema - owner: rag. Documents, chunks (pgvector + full-text), runbooks, history.

Permission-aware retrieval (ADR-006): every chunk carries `allowed_groups`, copied from
its document by a trigger, so ONE query can do ANN/FTS ranking + the permission filter.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import (
    EMBEDDING_DIM,
    EMPTY_JSON,
    Base,
    created_at_col,
    in_list,
    updated_at_col,
    uuid_pk,
)

SCHEMA = "rag"
SOURCES = ("markdown", "pdf", "txt", "html", "json", "code", "runbook", "incident")
SENSITIVITY = ("INTERNAL", "CONFIDENTIAL", "RESTRICTED")
HNSW_OPTS = {"m": 16, "ef_construction": 64}


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(in_list("source", SOURCES), name="source_valid"),
        CheckConstraint(in_list("sensitivity", SENSITIVITY), name="sensitivity_valid"),
        # Default DENY: a document with no allowed groups cannot be stored at all.
        CheckConstraint("cardinality(allowed_groups) >= 1", name="has_allowed_groups"),
        UniqueConstraint("source_uri", "version"),
        # serves: admin/ACL views "documents visible to group X"
        Index("ix_documents_allowed_groups", "allowed_groups", postgresql_using="gin"),
        # serves: browse by owning department / team
        Index("ix_documents_department_owner", "department", "owner"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)  # normalised, PII-scrubbed text
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="INTERNAL")
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint(
            "embedding IS NULL OR embedding_model IS NOT NULL", name="embedding_has_model"
        ),
        UniqueConstraint("document_id", "chunk_index"),  # also serves FK + ordered reads
        # serves: semantic (ANN) retrieval, cosine distance
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with=HNSW_OPTS,
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # serves: keyword retrieval (error codes, class names) - the "BM25 side" of hybrid
        Index("ix_document_chunks_tsv", "tsv", postgresql_using="gin"),
        # serves: permission filter allowed_groups && <caller groups> in the same statement as ranking
        Index("ix_document_chunks_allowed_groups", "allowed_groups", postgresql_using="gin"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, content)", persisted=True)
    )
    # Denormalised from documents.allowed_groups by trigger rag.sync_chunk_acl - never set
    # by application code. Lets ANN + FTS + permission filter run in one statement.
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = created_at_col()


class Runbook(Base):
    __tablename__ = "runbooks"
    __table_args__ = (
        UniqueConstraint("runbook_key", "version"),
        CheckConstraint("runbook_key ~ '^RUNBOOK-[A-Z0-9]+-[0-9]{3}$'", name="key_format"),
        CheckConstraint("jsonb_typeof(steps) = 'array'", name="steps_is_array"),
        # serves: "runbooks for service X"
        Index("ix_runbooks_service_keys", "service_keys", postgresql_using="gin"),
        # serves: FK lookup (runbook -> its indexed document)
        Index("ix_runbooks_document_id", "document_id"),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    runbook_key: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    service_keys: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.documents.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()


class HistoricalIncident(Base):
    """Closed past incidents (Feature 8). root_cause_category doubles as eval ground truth."""

    __tablename__ = "historical_incidents"
    __table_args__ = (
        CheckConstraint("incident_key ~ '^INC-[0-9]+$'", name="key_format"),
        CheckConstraint("severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4')", name="severity_valid"),
        CheckConstraint("resolved_at >= occurred_at", name="resolved_after_occurred"),
        CheckConstraint("cardinality(allowed_groups) >= 1", name="has_allowed_groups"),
        CheckConstraint(
            "embedding IS NULL OR embedding_model IS NOT NULL", name="embedding_has_model"
        ),
        # serves: similar-incident semantic search
        Index(
            "ix_historical_incidents_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with=HNSW_OPTS,
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # serves: keyword search on error codes / symptoms
        Index("ix_historical_incidents_tsv", "tsv", postgresql_using="gin"),
        # serves: "past incidents of service X" (array containment)
        Index("ix_historical_incidents_service_keys", "service_keys", postgresql_using="gin"),
        # serves: recency filter / timeline of past incidents
        Index("ix_historical_incidents_occurred_at", text("occurred_at DESC")),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    incident_key: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause_category: Mapped[str] = mapped_column(String(60), nullable=False)
    remediation: Mapped[str] = mapped_column(Text, nullable=False)
    service_keys: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english'::regconfig, title || ' ' || summary || ' ' || root_cause)",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = created_at_col()
