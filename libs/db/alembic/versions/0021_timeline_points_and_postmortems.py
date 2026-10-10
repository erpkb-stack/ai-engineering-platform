"""Phase 12 (ADR-022): agent-observed timeline points and generated postmortems.

- incident_events.dedupe_key: set for points an agent observed ("METRIC|p95_latency_ms|
  2026-10-02T09:49:00+00:00"); unique per incident -> POST /timeline is idempotent and a
  second investigation of the same incident does not duplicate the timeline.
- incident.postmortems: the GENERATED parts (model sections, action items, negative evidence),
  one per investigation. The document is assembled at read time.
Additive: old rows keep dedupe_key NULL; old code ignores the new table.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EV_IDX = "uq_incident_events_incident_id_dedupe_key"
PM_IDX = "uq_postmortems_incident_id_investigation_id"


def upgrade() -> None:
    op.add_column(
        "incident_events",
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        schema="incident",
    )
    op.create_index(
        EV_IDX,
        "incident_events",
        ["incident_id", "dedupe_key"],
        unique=True,
        schema="incident",
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.execute(
        f"COMMENT ON INDEX incident.{EV_IDX} IS 'enforces: one timeline point per observation "
        "per incident, whichever investigation reported it (idempotent POST /timeline)'"
    )
    op.create_table(
        "postmortems",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("written_by", sa.String(length=8), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("prompt_version", sa.SmallInteger(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "written_by IN ('llm', 'code')", name=op.f("ck_postmortems_written_by_valid")
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incident.incidents.id"],
            name=op.f("fk_postmortems_incident_id_incidents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_postmortems")),
        schema="incident",
    )
    op.create_index(
        PM_IDX, "postmortems", ["incident_id", "investigation_id"], unique=True,
        schema="incident",
    )  # fmt: skip
    op.execute(
        f"COMMENT ON INDEX incident.{PM_IDX} IS 'enforces: one postmortem per investigation "
        "(idempotent POST); serves: latest postmortem of an incident + FK lookup'"
    )


def downgrade() -> None:
    op.drop_index(PM_IDX, table_name="postmortems", schema="incident")
    op.drop_table("postmortems", schema="incident")
    op.drop_index(EV_IDX, table_name="incident_events", schema="incident")
    op.drop_column("incident_events", "dedupe_key", schema="incident")
