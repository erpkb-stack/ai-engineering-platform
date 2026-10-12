"""Phase 15 (ADR-025): orchestrator.release_assessments - advisory release-risk reports.

One row per request (each run is a new snapshot: the data may change between runs). Owned by
the orchestrator (no new service); table grants come from the schema's default privileges
(0001). Additive. Downgrade drops the table AND its rows: assessments are advice, not records
another table depends on.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_COMMENTS = {
    "orchestrator.ix_release_assessments_service_key_requested_at":
        "serves: risk history of service X, latest first (GET /release-risk?service_key=)",
    "orchestrator.ix_release_assessments_deploy_key":
        "serves: assessments that targeted deploy X (GET /release-risk?deploy_key=)",
    "orchestrator.uq_release_assessments_requester_idempotency_key":
        "enforces: a retried POST with the same Idempotency-Key returns the first report",
}  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "release_assessments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("requested_by", sa.String(length=120), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("repository", sa.String(length=120), nullable=True),
        sa.Column("pull_request", sa.Integer(), nullable=True),
        sa.Column("deploy_key", sa.String(length=24), nullable=True),
        sa.Column("service_key", sa.String(length=100), nullable=True),
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("head_sha", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("band", sa.String(length=8), nullable=True),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            "(target_kind = 'deploy' AND deploy_key IS NOT NULL AND pull_request IS NULL) OR "
            "(target_kind = 'pull_request' AND repository IS NOT NULL AND pull_request IS NOT NULL "
            "AND deploy_key IS NULL) OR "
            "(target_kind = 'range' AND service_key IS NOT NULL AND base_sha IS NOT NULL "
            "AND head_sha IS NOT NULL AND deploy_key IS NULL AND pull_request IS NULL)",
            name=op.f("ck_release_assessments_target_shape"),
        ),
        sa.CheckConstraint(
            "band IS NULL OR band IN ('LOW', 'MEDIUM', 'HIGH')",
            name=op.f("ck_release_assessments_band_valid"),
        ),
        sa.CheckConstraint(
            "status = 'FAILED' OR band IS NOT NULL",
            name=op.f("ck_release_assessments_band_unless_failed"),
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'PARTIAL', 'FAILED')",
            name=op.f("ck_release_assessments_status_valid"),
        ),
        sa.CheckConstraint(
            "target_kind IN ('pull_request', 'range', 'deploy')",
            name=op.f("ck_release_assessments_target_kind_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_assessments")),
        schema="orchestrator",
    )
    op.create_index(
        "ix_release_assessments_deploy_key",
        "release_assessments",
        ["deploy_key"],
        unique=False,
        schema="orchestrator",
    )
    op.create_index(
        "ix_release_assessments_service_key_requested_at",
        "release_assessments",
        ["service_key", sa.literal_column("requested_at DESC")],
        unique=False,
        schema="orchestrator",
    )
    op.create_index(
        "uq_release_assessments_requester_idempotency_key",
        "release_assessments",
        ["requested_by", "idempotency_key"],
        unique=True,
        schema="orchestrator",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    for index, why in INDEX_COMMENTS.items():
        op.execute(f"COMMENT ON INDEX {index} IS '{why}'")


def downgrade() -> None:
    op.drop_index(
        "uq_release_assessments_requester_idempotency_key",
        table_name="release_assessments",
        schema="orchestrator",
    )
    op.drop_index(
        "ix_release_assessments_service_key_requested_at",
        table_name="release_assessments",
        schema="orchestrator",
    )
    op.drop_index(
        "ix_release_assessments_deploy_key", table_name="release_assessments", schema="orchestrator"
    )
    op.drop_table("release_assessments", schema="orchestrator")
