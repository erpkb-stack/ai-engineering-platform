"""Phase 9 (ADR-019): delegation grants + events, LangGraph checkpoint tables, investigation
columns.

identity.delegation_grants / delegation_events (owner: api)
- A grant lets ONE service act for ONE user in ONE investigation until expires_at. Tokens are
  minted from it after re-reading the user's roles; the grant itself is not a credential.
- delegation_events is append-only for svc_api (UPDATE/DELETE/TRUNCATE revoked): the history
  of who was allowed to act for whom must not be rewritable by the service that issues it.
- svc_api cannot DELETE grants either: revocation is an UPDATE that keeps the row.
- Review finding: FK cascades run as the table OWNER, so REVOKE alone did not protect the
  history - a DELETE on identity.users cascaded through grants into events. Now: grants ->
  users is ON DELETE RESTRICT, events have no FK at all (soft refs), and svc_api loses DELETE
  on identity.users (users are deactivated, never deleted, by the service).

orchestrator.checkpoint* (owner: orchestrator)
- Exactly the DDL of langgraph-checkpoint-postgres (MIGRATIONS v0-v9), created here so the
  service never needs CREATE on its schema and Alembic stays the single stream. The library's
  migration table is filled so `setup()` would be a no-op; we never call it.
  tests/unit pins the library's migration count: a new library migration fails CI until it
  is mirrored in a new revision.

orchestrator.investigations: delegation_grant_id (soft ref to identity), deadline_at, error
(was stuffed into plan JSON in Phase 8). Additive, nullable: Phase 8 rows stay valid.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORCH = "orchestrator"
CHECKPOINT_MIGRATIONS = 10  # len(langgraph.checkpoint.postgres.base.MIGRATIONS) at 3.1.x

INDEX_COMMENTS = {
    "identity.ix_delegation_grants_user_id": "serves: grants of a user (revoke on deactivation, "
    "admin view); supports the FK cascade",
    "identity.ix_delegation_events_grant_id_created_at": "serves: ordered history of one grant",
    f"{ORCH}.checkpoints_thread_id_idx": "langgraph: checkpoints of one investigation (thread)",
    f"{ORCH}.checkpoint_blobs_thread_id_idx": "langgraph: channel blobs of one thread",
    f"{ORCH}.checkpoint_writes_thread_id_idx": "langgraph: pending writes of one thread",
}


def upgrade() -> None:
    # ---------------------------------------------------------------- identity
    op.create_table(
        "delegation_grants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("last_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_issued", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "actor LIKE 'service:%%'", name=op.f("ck_delegation_grants_actor_is_service")
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name=op.f("ck_delegation_grants_revoked_has_reason"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_delegation_grants_expires_after_created")
        ),
        sa.CheckConstraint(
            "tokens_issued >= 0", name=op.f("ck_delegation_grants_tokens_issued_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name=op.f("fk_delegation_grants_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delegation_grants")),
        sa.UniqueConstraint("investigation_id", name=op.f("uq_delegation_grants_investigation_id")),
        schema="identity",
    )
    op.create_index(
        "ix_delegation_grants_user_id", "delegation_grants", ["user_id"], schema="identity"
    )
    op.create_table(
        "delegation_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("grant_id", sa.UUID(), nullable=True),
        sa.Column("investigation_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("event", sa.String(length=12), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event IN ('CREATED', 'ISSUED', 'DENIED', 'REVOKED')",
            name=op.f("ck_delegation_events_event_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delegation_events")),
        schema="identity",
    )
    op.create_index(
        "ix_delegation_events_grant_id_created_at",
        "delegation_events",
        ["grant_id", "created_at"],
        schema="identity",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON identity.delegation_events FROM svc_api")
    op.execute("REVOKE DELETE, TRUNCATE ON identity.delegation_grants FROM svc_api")
    op.execute("REVOKE DELETE, TRUNCATE ON identity.users FROM svc_api")

    # ---------------------------------------------------------------- orchestrator
    op.add_column("investigations", sa.Column("delegation_grant_id", sa.UUID()), schema=ORCH)
    op.add_column(
        "investigations", sa.Column("deadline_at", sa.DateTime(timezone=True)), schema=ORCH
    )
    op.add_column("investigations", sa.Column("error", sa.Text()), schema=ORCH)

    op.create_table(
        "checkpoint_migrations",
        sa.Column("v", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("v", name=op.f("pk_checkpoint_migrations")),
        schema=ORCH,
    )
    op.create_table(
        "checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id", name=op.f("pk_checkpoints")
        ),
        schema=ORCH,
    )
    op.create_index("checkpoints_thread_id_idx", "checkpoints", ["thread_id"], schema=ORCH)
    op.create_table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("blob", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint(
            "thread_id", "checkpoint_ns", "channel", "version", name=op.f("pk_checkpoint_blobs")
        ),
        schema=ORCH,
    )
    op.create_index(
        "checkpoint_blobs_thread_id_idx", "checkpoint_blobs", ["thread_id"], schema=ORCH
    )
    op.create_table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            name=op.f("pk_checkpoint_writes"),
        ),
        schema=ORCH,
    )
    op.create_index(
        "checkpoint_writes_thread_id_idx", "checkpoint_writes", ["thread_id"], schema=ORCH
    )
    op.execute(
        f"INSERT INTO {ORCH}.checkpoint_migrations (v) "
        f"SELECT generate_series(0, {CHECKPOINT_MIGRATIONS - 1})"
    )

    for name, comment in INDEX_COMMENTS.items():
        op.execute(f"COMMENT ON INDEX {name} IS '{comment}'")


def downgrade() -> None:
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
        op.drop_table(table, schema=ORCH)
    op.drop_column("investigations", "error", schema=ORCH)
    op.drop_column("investigations", "deadline_at", schema=ORCH)
    op.drop_column("investigations", "delegation_grant_id", schema=ORCH)
    op.drop_table("delegation_events", schema="identity")
    op.drop_table("delegation_grants", schema="identity")
    op.execute("GRANT DELETE ON identity.users TO svc_api")
