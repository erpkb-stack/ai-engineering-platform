"""Schemas, per-service roles, grants and shared trigger functions.

Revision ID: 0001
Revises:
Create Date: 2026-10-05

Design (ADR-013, docs/data-model.md):
- One schema per owning service. Each service gets a NOLOGIN group role `svc_<service>`
  with DML on ITS schema only. Phase 4+ creates LOGIN users that are members of these roles.
- `aeoi_readonly` (created by the Docker init script, used by Claude Code's Postgres MCP)
  gets SELECT on operational schemas, but NOT on `identity` (people) or `audit`.
- Roles are cluster-wide objects: downgrade REVOKES grants in this database but never
  drops roles, because another database on the same server may still use them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# schema -> role of its owning service
SCHEMA_ROLES = {
    "identity": "svc_api",
    "incident": "svc_incident",
    "orchestrator": "svc_orchestrator",
    "rag": "svc_rag",
    "devdata": "svc_tool_gateway",
    "tools": "svc_tool_gateway",
    "llm": "svc_llm_gateway",
    "audit": "svc_audit",
    "eval": "svc_evaluation",
}
READONLY_SCHEMAS = ("incident", "orchestrator", "rag", "devdata", "tools", "llm", "eval")
OWNER = "CURRENT_USER"  # the migration user owns all tables (aeoi locally)


def _ensure_role(role: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
            CREATE ROLE {role} NOLOGIN;
          END IF;
        END $$;
        """
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    _ensure_role("aeoi_readonly")  # normally created by the Docker init script

    for schema, role in SCHEMA_ROLES.items():
        _ensure_role(role)
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        op.execute(f"COMMENT ON SCHEMA {schema} IS 'owner service role: {role}'")
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        if schema == "audit":
            # Append-only: insert + read, never update/delete.
            op.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
                f"GRANT SELECT, INSERT ON TABLES TO {role}"
            )
        else:
            op.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
            )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
        )

    for schema in READONLY_SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO aeoi_readonly")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
            "GRANT SELECT ON TABLES TO aeoi_readonly"
        )

    # Shared trigger function: keep updated_at honest without trusting app code.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          NEW.updated_at := now();
          RETURN NEW;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.set_updated_at()")
    for schema in READONLY_SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
            "REVOKE SELECT ON TABLES FROM aeoi_readonly"
        )
    for schema, role in SCHEMA_ROLES.items():
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
            f"REVOKE ALL ON TABLES FROM {role}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {schema} "
            f"REVOKE ALL ON SEQUENCES FROM {role}"
        )
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
