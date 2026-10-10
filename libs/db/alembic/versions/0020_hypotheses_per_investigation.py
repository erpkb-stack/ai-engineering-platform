"""Phase 11 (ADR-021): hypotheses belong to an investigation.

incident.hypotheses existed since Phase 3 with no link to the run that produced it, so a
re-delivered POST (orchestrator retry, resume after a crash) would insert duplicates. Added:
- investigation_id (soft ref to orchestrator.investigations - no cross-schema FK)
- hypothesis_key ("h1".."h12", unique per investigation) -> the POST is idempotent
- detail JSONB: cause kind, origin (code|critic), rubric, critic verdict, LLM explanation
Additive and nullable: Phase 3 rows (none in practice) stay valid; old code ignores them.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDX = "uq_hypotheses_investigation_id_hypothesis_key"


def upgrade() -> None:
    op.add_column(
        "hypotheses",
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="incident",
    )
    op.add_column(
        "hypotheses", sa.Column("hypothesis_key", sa.String(length=8), nullable=True),
        schema="incident",
    )  # fmt: skip
    op.add_column(
        "hypotheses",
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="incident",
    )
    op.create_index(
        IDX,
        "hypotheses",
        ["investigation_id", "hypothesis_key"],
        unique=True,
        schema="incident",
        postgresql_where=sa.text("investigation_id IS NOT NULL"),
    )
    op.execute(
        f"COMMENT ON INDEX incident.{IDX} IS 'enforces: one row per hypothesis key per "
        "investigation (idempotent POST); serves: hypotheses of one investigation'"
    )


def downgrade() -> None:
    op.drop_index(IDX, table_name="hypotheses", schema="incident")
    op.drop_column("hypotheses", "detail", schema="incident")
    op.drop_column("hypotheses", "hypothesis_key", schema="incident")
    op.drop_column("hypotheses", "investigation_id", schema="incident")
