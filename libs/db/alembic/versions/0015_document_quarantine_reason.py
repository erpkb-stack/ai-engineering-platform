"""rag.documents.quarantine_reason: a quarantined document must say why.

Phase 6 ingestion quarantines documents that trip the prompt-injection detector. A bare
boolean can't be reviewed ("why is this hidden?"), so the reason is stored and a CHECK keeps
the two columns consistent. Additive and nullable: safe on a live table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-05 22:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("quarantine_reason", sa.Text()), schema="rag")
    op.create_check_constraint(
        "quarantine_has_reason",
        "documents",
        "quarantined = (quarantine_reason IS NOT NULL)",
        schema="rag",
    )


def downgrade() -> None:
    op.drop_constraint("quarantine_has_reason", "documents", schema="rag", type_="check")
    op.drop_column("documents", "quarantine_reason", schema="rag")
