"""Phase 10 (ADR-020): investigation status PARTIAL.

PARTIAL = some agents succeeded, some failed: the found evidence is kept and the missing
sources are named in `error`. COMPLETE would hide a missing source ("the deploy agent found
nothing" vs "the deploy agent did not run"); FAILED would throw away good evidence.

Locking, honestly: DROP/ADD CONSTRAINT take ACCESS EXCLUSIVE, and Alembic runs this file in
ONE transaction, so that lock is held until commit - including the VALIDATE scan. NOT VALID
does not help inside one transaction. Acceptable here: one row per investigation (small, and
the scan is a few ms). [Prod, large table] split it: revision A drops + adds NOT VALID
(instant), revision B validates (SHARE UPDATE EXCLUSIVE: reads and writes continue).
Rollout: the new set is a SUPERSET, every existing row passes, and old code never writes
PARTIAL, so old and new code can run side by side.
Downgrade: old code knows no PARTIAL. Such rows become FAILED (never COMPLETE: a source was
missing) and their `error` says so - the change is recorded in the row, not silent.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "orchestrator.investigations"
OLD = "('RUNNING', 'AWAITING_REVIEW', 'COMPLETE', 'INCONCLUSIVE', 'FAILED', 'CANCELLED')"
NEW = "('RUNNING', 'AWAITING_REVIEW', 'COMPLETE', 'PARTIAL', 'INCONCLUSIVE', 'FAILED', 'CANCELLED')"


def _swap(statuses: str) -> None:
    op.execute(f"ALTER TABLE {T} DROP CONSTRAINT ck_investigations_status_valid")
    op.execute(
        f"ALTER TABLE {T} ADD CONSTRAINT ck_investigations_status_valid "
        f"CHECK (status IN {statuses}) NOT VALID"
    )
    op.execute(f"ALTER TABLE {T} VALIDATE CONSTRAINT ck_investigations_status_valid")


def upgrade() -> None:
    _swap(NEW)


def downgrade() -> None:
    op.execute(
        f"UPDATE {T} SET status = 'FAILED', "
        "error = left('[was PARTIAL, downgraded by 0019] ' || coalesce(error, ''), 1000) "
        "WHERE status = 'PARTIAL'"
    )
    _swap(OLD)
