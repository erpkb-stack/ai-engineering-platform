"""Phase 23a (ADR-035): an investigation keeps its trace context; a narrow reader for Grafana.

1. orchestrator.investigations gets `trace_context` (W3C traceparent) and `correlation_id`:
   a RESUMED run had neither (no request of its own), so it could not be followed.
   Additive, nullable: old rows simply start a trace of their own.
2. Role `obs_reader` (NOLOGIN; the login `grafana_ro` is made by `make db-users`): SELECT on
   ONLY the columns the dashboards read - no incident text, no plan, no error text, no user id.
   Column grants, so a new column is NOT readable until a migration says so.

Downgrade drops both columns and revokes the grants; the role stays (server-wide, see 0024).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "obs_reader"
# table -> the columns a dashboard may read (tests/unit checks the dashboards use only these)
READABLE = {
    "orchestrator.investigations": (
        "id", "incident_id", "status", "started_at", "finished_at", "budget_usd", "spent_usd",
    ),
    "llm.model_usage": (
        "provider", "model", "operation", "agent_name", "investigation_id", "input_tokens",
        "output_tokens", "cached_tokens", "latency_ms", "cost_usd", "status", "created_at",
    ),
}  # fmt: skip


def upgrade() -> None:
    op.add_column(
        "investigations", sa.Column("trace_context", sa.String(64)), schema="orchestrator"
    )
    op.add_column(
        "investigations", sa.Column("correlation_id", sa.String(80)), schema="orchestrator"
    )
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} NOLOGIN;
          END IF;
        END $$;
        """
    )
    for table, columns in READABLE.items():
        schema = table.split(".")[0]
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {ROLE}")
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON {table} TO {ROLE}")


def downgrade() -> None:
    for table, columns in READABLE.items():
        schema = table.split(".")[0]
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON {table} FROM {ROLE}")
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {ROLE}")
    # the role is NOT dropped (the 0024 rule): roles belong to the whole SERVER, and another
    # database of it (the dev DB beside a test DB) still grants obs_reader rights - DROP ROLE
    # then fails and leaves this database half-downgraded (CI, 2026-10-21: 29 integration
    # tests). Without grants here it can read nothing in this database.
    op.drop_column("investigations", "correlation_id", schema="orchestrator")
    op.drop_column("investigations", "trace_context", schema="orchestrator")
