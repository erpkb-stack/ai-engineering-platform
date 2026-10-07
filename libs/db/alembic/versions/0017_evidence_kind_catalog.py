"""incident.evidence: allow kind CATALOG (service-catalog facts from query_service_catalog).

Phase 8 made tool-gateway evidence ids kind-prefixed (`<KIND>-<tool_call_hex>-<n>`) so agents
can cite them directly as findings evidence and incident.evidence accepts them unchanged
(`key LIKE kind || '-%'`). The catalog tool needed a kind; none of the existing ones fit.
Additive: the new CHECK accepts every row the old one accepted.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("LOG", "METRIC", "TRACE", "DEPLOY", "CONFIG", "COMMIT", "PR", "DOC", "RUNBOOK",
       "INCIDENT", "ALERT")  # fmt: skip
NEW = (*OLD, "CATALOG")


def _check(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"


def _replace(kinds: tuple[str, ...]) -> None:
    # raw SQL: the naming convention would turn "ck_evidence_kind_valid" into a double prefix
    op.execute("ALTER TABLE incident.evidence DROP CONSTRAINT ck_evidence_kind_valid")
    op.execute(
        f"ALTER TABLE incident.evidence ADD CONSTRAINT ck_evidence_kind_valid CHECK ({_check(kinds)})"
    )


def upgrade() -> None:
    _replace(NEW)


def downgrade() -> None:
    # fails (on purpose) if CATALOG rows exist: delete them first, never silently drop data
    _replace(OLD)
