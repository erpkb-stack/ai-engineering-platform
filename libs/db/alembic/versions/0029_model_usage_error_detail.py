"""llm.model_usage.error_detail: WHERE a structured output failed - schema paths only.

Phase 28b (ADR-042): 11 historical_incident StructuredOutputError rows could not be diagnosed,
because only the error CLASS was stored, and the agent saw a 40 s timeout instead (the fallback
model ran out of time). This column holds the failing SCHEMA paths with their keyword (e.g.
`properties/matches/items/required`), `cut_off:max_tokens`, or "cancelled: tokens unknown"
for an attempt the deadline cut - never a message, never a value
from the model's output (jsonschema messages quote the instance). Additive and nullable.
obs_reader gets no grant on it (0026 grants columns by name).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_usage", sa.Column("error_detail", sa.String(300)), schema="llm")


def downgrade() -> None:
    op.drop_column("model_usage", "error_detail", schema="llm")
