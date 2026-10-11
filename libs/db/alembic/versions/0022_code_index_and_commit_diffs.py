"""Phase 14 (ADR-024): code index, commit diffs, evidence kind CODE.

- devdata.commit_diffs: what each synthetic commit changed (unified diff + file after), as a
  code host's commit API would return it. Filled by `make db-seed` (aeoi_synth.codegen).
- rag.code_files / code_symbols / code_refs: the repositories at HEAD parsed by tree-sitter
  (`make code-index`). ACL per file, filtered IN the SQL like every rag table.
- incident.evidence.kind: + CODE (a code location found by find_references).
Additive; the new CHECK accepts every row the old one accepted.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_KINDS = ("LOG", "METRIC", "TRACE", "DEPLOY", "CONFIG", "COMMIT", "PR", "DOC", "RUNBOOK",
             "INCIDENT", "ALERT", "CATALOG")  # fmt: skip
NEW_KINDS = (*OLD_KINDS, "CODE")
INDEX_COMMENTS = {
    "rag.ix_code_files_service_key": "serves: files of service X (impact grouped by service)",
    "rag.ix_code_symbols_name": "serves: definition lookup by simple name",
    "rag.ix_code_symbols_file_id": "serves: FK lookup + delete-on-reindex of one file",
    "rag.ix_code_refs_name": "serves: change impact - who uses name X (find_references)",
    "rag.ix_code_refs_file_id": "serves: FK lookup + delete-on-reindex of one file",
}


def _kinds(kinds: tuple[str, ...]) -> None:
    # raw SQL as in 0017: the naming convention would double the "ck_" prefix
    op.execute("ALTER TABLE incident.evidence DROP CONSTRAINT ck_evidence_kind_valid")
    check = "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"
    op.execute(
        f"ALTER TABLE incident.evidence ADD CONSTRAINT ck_evidence_kind_valid CHECK ({check})"
    )


def upgrade() -> None:
    op.create_table(
        "code_files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repository", sa.String(length=120), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("service_key", sa.String(length=100), nullable=True),
        sa.Column("language", sa.String(length=12), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("allowed_groups", postgresql.ARRAY(sa.String(length=80)), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "language IN ('java', 'python', 'go')", name=op.f("ck_code_files_language_valid")
        ),
        sa.CheckConstraint(
            "cardinality(allowed_groups) >= 1", name=op.f("ck_code_files_has_allowed_groups")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_files")),
        sa.UniqueConstraint("repository", "path", name=op.f("uq_code_files_repository_path")),
        schema="rag",
    )
    op.create_index(
        "ix_code_files_service_key", "code_files", ["service_key"], unique=False, schema="rag"
    )
    op.create_table(
        "commit_diffs",
        sa.Column("sha", sa.String(length=40), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("patch", sa.Text(), nullable=False),
        sa.Column("content_after", sa.Text(), nullable=False),
        sa.Column("additions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deletions", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["sha"],
            ["devdata.commits.sha"],
            name=op.f("fk_commit_diffs_sha_commits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("sha", "path", name=op.f("pk_commit_diffs")),
        schema="devdata",
    )
    op.create_table(
        "code_refs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("enclosing", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "kind IN ('call', 'type', 'field')", name=op.f("ck_code_refs_kind_valid")
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["rag.code_files.id"],
            name=op.f("fk_code_refs_file_id_code_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_refs")),
        schema="rag",
    )
    op.create_index("ix_code_refs_file_id", "code_refs", ["file_id"], unique=False, schema="rag")
    op.create_index("ix_code_refs_name", "code_refs", ["name"], unique=False, schema="rag")
    op.create_table(
        "code_symbols",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("container", sa.String(length=200), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('class', 'interface', 'struct', 'method', 'function', 'field', 'constant')",
            name=op.f("ck_code_symbols_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["rag.code_files.id"],
            name=op.f("fk_code_symbols_file_id_code_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_symbols")),
        schema="rag",
    )
    op.create_index(
        "ix_code_symbols_file_id", "code_symbols", ["file_id"], unique=False, schema="rag"
    )
    op.create_index("ix_code_symbols_name", "code_symbols", ["name"], unique=False, schema="rag")
    for index, why in INDEX_COMMENTS.items():
        op.execute(f"COMMENT ON INDEX {index} IS '{why}'")
    _kinds(NEW_KINDS)


def downgrade() -> None:
    # fails (on purpose) if CODE evidence rows exist: delete them first, never drop data silently
    _kinds(OLD_KINDS)
    op.drop_index("ix_code_symbols_name", table_name="code_symbols", schema="rag")
    op.drop_index("ix_code_symbols_file_id", table_name="code_symbols", schema="rag")
    op.drop_table("code_symbols", schema="rag")
    op.drop_index("ix_code_refs_name", table_name="code_refs", schema="rag")
    op.drop_index("ix_code_refs_file_id", table_name="code_refs", schema="rag")
    op.drop_table("code_refs", schema="rag")
    op.drop_table("commit_diffs", schema="devdata")
    op.drop_index("ix_code_files_service_key", table_name="code_files", schema="rag")
    op.drop_table("code_files", schema="rag")
