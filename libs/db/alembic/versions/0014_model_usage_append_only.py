"""llm.model_usage is append-only for the llm-gateway role.

Why: cost rows drive the per-investigation budget and the cost KPIs. If the service can
UPDATE/DELETE them, a bug (or an injected query) can erase spend and bypass the budget.
The gateway only ever INSERTs and SELECTs (sum for budgets). Corrections are new rows.
prompt_versions keeps full DML (it is a registry mirror that the gateway re-syncs).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-05 15:40:00+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON llm.model_usage FROM svc_llm_gateway")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE ON llm.model_usage TO svc_llm_gateway")
