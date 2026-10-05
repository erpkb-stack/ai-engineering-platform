"""Declarative base, naming convention and shared column helpers.

Conventions (docs/data-model.md):
- UUIDv7 primary keys generated in the app (time-ordered -> good B-tree locality).
- Status / kind columns are TEXT + CHECK, not Postgres ENUM types: adding a value is a
  one-line constraint swap instead of an ALTER TYPE that can't run inside a transaction.
- No foreign keys across schemas: each schema has one owning service; cross-service
  references are plain ids/keys, resolved through APIs or events.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aeoi_common.ids import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Schema -> owning service. The only service allowed to write that schema.
SCHEMA_OWNERS: dict[str, str] = {
    "identity": "api",
    "incident": "incident-service",
    "orchestrator": "orchestrator",
    "rag": "rag",
    "devdata": "tool-gateway",
    "tools": "tool-gateway",
    "llm": "llm-gateway",
    "audit": "audit",
    "eval": "evaluation",
}

EMBEDDING_DIM = 768  # nomic-embed-text; a model change with another dim = new column + backfill


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_col() -> Mapped[datetime]:
    # Kept current by the set_updated_at() trigger (migration 0002), not by the app.
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def in_list(column: str, values: list[str] | tuple[str, ...]) -> str:
    """SQL for a CHECK constraint: column IN ('A','B')."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


EMPTY_JSON = text("'{}'::jsonb")
EMPTY_TEXT_ARRAY = text("'{}'::text[]")
