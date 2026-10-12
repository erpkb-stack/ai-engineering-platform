"""Phase 16 (ADR-026): human approval -> controlled execution.

- incident.approvals: source / investigation_id (who asked: an SRE, or an investigation for
  the user who started it), outcome / outcome_detail / tool_call_id / executed_at (what the
  execution did), decision EXPIRED (set by the system, no decider), separation of duties as a
  CHECK (approver is never the requester).
- tools.tool_calls: status PENDING = the recorded INTENT of a consequential call, written
  before it executes; one intent and at most one OK per approval (unique, partial).
- role svc_cd: INSERT on devdata.deployments ONLY - the simulated CD system the rollback tool
  writes to (login cd_svc via `make db-users`). It cannot read, update or delete anything.

Additive. Downgrade works WITH data present (database rule; precedent 0019): values that do
not exist before 0024 are mapped and SAID so, never silently rewritten -
  decision EXPIRED   -> REJECTED by the nil-uuid "system" decider, reason "[was EXPIRED, ...]"
                        (NOT undecided: a later request for the same args may be pending, and
                        the pending-request unique index would fail - review H4)
  tool call PENDING  -> ERROR, denial_reason "[was PENDING intent, downgraded by 0024]"
The outcome/source columns are dropped: what ran stays in incident_events (ActionExecuted,
with the outcome) and in the audit service.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_COMMENTS = {
    "tools.uq_tool_calls_approval_intent":
        "enforces: one recorded intent per approval (a replay cannot start a second execution)",
    "tools.uq_tool_calls_approval_ok": "enforces: at most one successful execution per approval",
}  # fmt: skip
OLD_STATUSES = "status IN ('OK', 'DENIED', 'ERROR', 'TIMEOUT')"
NEW_STATUSES = "status IN ('OK', 'DENIED', 'ERROR', 'TIMEOUT', 'PENDING')"
OLD_DECISIONS = "decision IS NULL OR decision IN ('APPROVED', 'REJECTED', 'MORE_INFO')"
NEW_DECISIONS = "decision IS NULL OR decision IN ('APPROVED', 'REJECTED', 'MORE_INFO', 'EXPIRED')"
OLD_TOGETHER = (
    "(decision IS NULL) = (decided_at IS NULL) AND (decision IS NULL) = (decided_by IS NULL)"
)
NEW_TOGETHER = (
    "(decision IS NULL) = (decided_at IS NULL) AND "
    "(decision IS NULL OR decision = 'EXPIRED') = (decided_by IS NULL)"
)


def _replace(table: str, name: str, check: str) -> None:
    # raw SQL as in 0017/0022: the naming convention would double the "ck_" prefix
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check})")


def upgrade() -> None:
    a = "approvals"
    op.add_column(a, sa.Column("source", sa.String(16), server_default="manual", nullable=False),
                  schema="incident")  # fmt: skip
    op.add_column(a, sa.Column("investigation_id", sa.UUID(), nullable=True), schema="incident")
    op.add_column(a, sa.Column("outcome", sa.String(12), nullable=True), schema="incident")
    op.add_column(a, sa.Column("outcome_detail", postgresql.JSONB(astext_type=sa.Text()),
                               server_default=sa.text("'{}'::jsonb"), nullable=False),
                  schema="incident")  # fmt: skip
    op.add_column(a, sa.Column("tool_call_id", sa.UUID(), nullable=True), schema="incident")
    op.add_column(a, sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
                  schema="incident")  # fmt: skip
    _replace("incident.approvals", "ck_approvals_decision_valid", NEW_DECISIONS)
    _replace("incident.approvals", "ck_approvals_decision_fields_together", NEW_TOGETHER)
    op.execute("ALTER TABLE incident.approvals ADD CONSTRAINT ck_approvals_approver_is_not_requester"
               " CHECK (decided_by IS NULL OR requested_by <> 'user:' || decided_by::text)")  # fmt: skip
    op.execute("ALTER TABLE incident.approvals ADD CONSTRAINT ck_approvals_source_valid"
               " CHECK (source IN ('investigation', 'manual'))")  # fmt: skip
    op.execute("ALTER TABLE incident.approvals ADD CONSTRAINT ck_approvals_outcome_valid"
               " CHECK (outcome IS NULL OR outcome IN ('EXECUTED', 'FAILED'))")  # fmt: skip
    op.execute("ALTER TABLE incident.approvals ADD CONSTRAINT ck_approvals_outcome_after_consume"
               " CHECK (outcome IS NULL OR consumed_at IS NOT NULL)")  # fmt: skip

    _replace("tools.tool_calls", "ck_tool_calls_status_valid", NEW_STATUSES)
    op.create_index("uq_tool_calls_approval_intent", "tool_calls", ["approval_id"], unique=True,
                    schema="tools", postgresql_where=sa.text("status = 'PENDING'"))  # fmt: skip
    op.create_index("uq_tool_calls_approval_ok", "tool_calls", ["approval_id"], unique=True,
                    schema="tools",
                    postgresql_where=sa.text("status = 'OK' AND approval_id IS NOT NULL"))  # fmt: skip
    for index, why in INDEX_COMMENTS.items():
        op.execute(f"COMMENT ON INDEX {index} IS '{why}'")

    # the simulated CD system: insert-only on deployments, nothing else
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_cd') THEN
            CREATE ROLE svc_cd NOLOGIN;
          END IF;
        END $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA devdata TO svc_cd")
    op.execute("GRANT INSERT ON devdata.deployments TO svc_cd")


def downgrade() -> None:
    op.execute(
        "UPDATE incident.approvals SET decision = 'REJECTED', "
        "decided_by = '00000000-0000-0000-0000-000000000000', decider_role = 'SYSTEM', "
        "reason = left('[was EXPIRED, downgraded by 0024] ' || coalesce(reason, ''), 1000) "
        "WHERE decision = 'EXPIRED'"
    )
    op.execute(
        "UPDATE tools.tool_calls SET status = 'ERROR', "
        "denial_reason = '[was PENDING intent, downgraded by 0024]' WHERE status = 'PENDING'"
    )
    op.execute("REVOKE ALL ON devdata.deployments FROM svc_cd")
    op.execute("REVOKE USAGE ON SCHEMA devdata FROM svc_cd")
    # the role is NOT dropped (as in 0001): roles belong to the whole server, and another
    # database of it (the dev DB beside a test DB) may still grant svc_cd rights - DROP ROLE
    # then fails (found on the Mac). Without grants here it can do nothing in this database.
    op.drop_index("uq_tool_calls_approval_ok", table_name="tool_calls", schema="tools")
    op.drop_index("uq_tool_calls_approval_intent", table_name="tool_calls", schema="tools")
    _replace("tools.tool_calls", "ck_tool_calls_status_valid", OLD_STATUSES)
    for name in ("outcome_after_consume", "outcome_valid", "source_valid",
                 "approver_is_not_requester"):  # fmt: skip
        op.execute(f"ALTER TABLE incident.approvals DROP CONSTRAINT ck_approvals_{name}")
    _replace("incident.approvals", "ck_approvals_decision_fields_together", OLD_TOGETHER)
    _replace("incident.approvals", "ck_approvals_decision_valid", OLD_DECISIONS)
    for col in ("executed_at", "tool_call_id", "outcome_detail", "outcome", "investigation_id",
                "source"):  # fmt: skip
        op.drop_column("approvals", col, schema="incident")
