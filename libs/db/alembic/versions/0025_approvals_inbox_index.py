"""Phase 17b: index for the approvals inbox (GET /approvals?status=PENDING).

The inbox lists undecided, unexpired requests across incidents, the one about to expire
first, and every open console polls it every 15 s. Expiry is recorded lazily (when someone
decides or re-requests), so undecided rows include long-expired ones: without this partial
index the query reads every undecided row ever created and sorts them (review M4).

Additive; the downgrade drops the index (no data involved).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_approvals_undecided_expires_at"


def upgrade() -> None:
    op.create_index(INDEX, "approvals", ["expires_at", "id"], schema="incident",
                    postgresql_where="decision IS NULL")  # fmt: skip
    op.execute(f"COMMENT ON INDEX incident.{INDEX} IS "
               "'serves: the approvals inbox - undecided requests, soonest expiry first'")  # fmt: skip


def downgrade() -> None:
    op.drop_index(INDEX, table_name="approvals", schema="incident")
