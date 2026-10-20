"""Phase 23b (ADR-035): the KPI and Tool-gateway dashboards may read a few more COLUMNS.

1. tools.tool_calls gets `denial_code`: a STORED generated column = the code before the colon
   in `denial_reason` ("missing_permission: needs code:read" -> "missing_permission"). The
   dashboard needs the code; the free-text detail stays unreadable for Grafana.
   [P] ADD COLUMN ... STORED rewrites the table under an exclusive lock: fine for a dev table of
   a few thousand rows. [Prod] add a plain column, backfill in batches, then a trigger or the
   gateway writes it.
2. obs_reader (0026) gets SELECT on ONLY these columns - no titles, no text, no payloads, no
   user ids, no action arguments:
   - incident.incidents: id, severity, status, created_at, resolved_at
   - incident.hypotheses: investigation_id, created_at
   - incident.approvals: decision/outcome times and kinds (not args, not reason, not who)
   - tools.tool_calls: tool, agent, side effect, status, denial_code, latency, attempt, time

Downgrade revokes the grants and drops the column (derived data only).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "obs_reader"
DENIAL_CODE_SQL = "NULLIF(split_part(denial_reason, ':', 1), '')"
# table -> the columns a dashboard may read (tests/unit checks the dashboards use only these)
READABLE = {
    "incident.incidents": ("id", "severity", "status", "created_at", "resolved_at"),
    "incident.hypotheses": ("investigation_id", "created_at"),
    "incident.approvals": (
        "id", "incident_id", "action", "source", "requested_at", "expires_at", "decision",
        "decider_role", "decided_at", "outcome", "executed_at",
    ),
    "tools.tool_calls": (
        "tool_name", "agent_name", "side_effect", "status", "denial_code", "latency_ms",
        "attempt", "created_at",
    ),
}  # fmt: skip


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tools.tool_calls ADD COLUMN denial_code varchar(64) "
        f"GENERATED ALWAYS AS ({DENIAL_CODE_SQL}) STORED"
    )
    op.execute(
        "COMMENT ON COLUMN tools.tool_calls.denial_code IS "
        "'The denial code only (derived); readable by obs_reader, the reason text is not'"
    )
    for table, columns in READABLE.items():
        schema = table.split(".")[0]
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {ROLE}")
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON {table} TO {ROLE}")


def downgrade() -> None:
    for table, columns in READABLE.items():
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON {table} FROM {ROLE}")
    for schema in sorted({t.split(".")[0] for t in READABLE}):
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {ROLE}")
    op.execute("ALTER TABLE tools.tool_calls DROP COLUMN denial_code")
