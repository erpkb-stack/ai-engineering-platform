"""audit partitions kept by the audit service; incident.processed_events dropped (Phase 30).

Architecture review, verified on the Mac 2026-10-30: `audit.ensure_partitions` ran ONCE, inside
0011 (partitions 2026-09..2026-12 exist). Nothing ran it again, so from 2027-01-01 every audit
row would land in `audit_events_default`, and creating that month's partition afterwards FAILS
(a default-partition row would fall inside it) - for every later month too.

- `ensure_partitions` v2: SECURITY DEFINER (the serving login has no DDL right on the table;
  the function runs as its owner, the migration user) with a pinned search_path; a month whose
  rows already sit in the default partition is SKIPPED with a WARNING instead of failing the
  whole call (the months after it are still created). EXECUTE: svc_audit only (not PUBLIC).
  The audit service calls it at start and every few hours (aeoi_audit.main.keep_partitions).
- `incident.processed_events` (0003) had no writer and no reader anywhere: dead schema. The
  real consumer dedupe is the audit insert's ON CONFLICT on (id, occurred_at) (ADR-029).

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

V2 = """
CREATE OR REPLACE FUNCTION audit.ensure_partitions(months_ahead int DEFAULT 3) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
DECLARE
  m date := date_trunc('month', now() - interval '1 month')::date;
  stop date := (date_trunc('month', now()) + make_interval(months => months_ahead))::date;
  created int := 0;
  part text;
BEGIN
  IF months_ahead < 0 OR months_ahead > 24 THEN
    RAISE EXCEPTION 'months_ahead must be 0..24, got %', months_ahead;
  END IF;
  WHILE m <= stop LOOP
    part := format('audit_events_y%sm%s', to_char(m, 'YYYY'), to_char(m, 'MM'));
    IF to_regclass('audit.' || part) IS NULL THEN
      IF EXISTS (SELECT 1 FROM audit.audit_events_default
                 WHERE occurred_at >= m AND occurred_at < (m + interval '1 month')) THEN
        RAISE WARNING 'audit partition % skipped: rows for that month are in audit_events_default',
          part;
      ELSE
        EXECUTE format(
          'CREATE TABLE audit.%I PARTITION OF audit.audit_events FOR VALUES FROM (%L) TO (%L)',
          part, m, (m + interval '1 month')::date);
        created := created + 1;
      END IF;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END $$;
"""

V1 = """
CREATE OR REPLACE FUNCTION audit.ensure_partitions(months_ahead int DEFAULT 2) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
  m date := date_trunc('month', now() - interval '1 month')::date;
  stop date := (date_trunc('month', now()) + make_interval(months => months_ahead))::date;
  created int := 0;
  part text;
BEGIN
  WHILE m <= stop LOOP
    part := format('audit_events_y%sm%s', to_char(m, 'YYYY'), to_char(m, 'MM'));
    IF to_regclass('audit.' || part) IS NULL THEN
      EXECUTE format(
        'CREATE TABLE audit.%I PARTITION OF audit.audit_events FOR VALUES FROM (%L) TO (%L)',
        part, m, (m + interval '1 month')::date);
      created := created + 1;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END $$;
"""


def upgrade() -> None:
    # v1 has a different DEFAULT: CREATE OR REPLACE cannot change it, so drop first
    op.execute("DROP FUNCTION audit.ensure_partitions(int)")
    op.execute(V2)
    op.execute(
        "COMMENT ON FUNCTION audit.ensure_partitions(int) IS "
        "'Creates monthly partitions from last month to N months ahead; skips (WARNING) a month "
        "whose rows are already in the default partition. Called by the audit service at start "
        "and every few hours (Phase 30).'"
    )
    op.execute("REVOKE ALL ON FUNCTION audit.ensure_partitions(int) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION audit.ensure_partitions(int) TO svc_audit")
    op.execute("SELECT audit.ensure_partitions(3)")
    op.drop_table("processed_events", schema="incident")


def downgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(length=80), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_processed_events")),
        schema="incident",
    )
    op.execute("DROP FUNCTION audit.ensure_partitions(int)")
    op.execute(V1)
    op.execute(
        "COMMENT ON FUNCTION audit.ensure_partitions(int) IS "
        "'Creates monthly partitions from last month to N months ahead. Run monthly (cron/K8s CronJob).'"
    )
