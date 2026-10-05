"""RBAC reference data: roles, permissions, role -> permission mapping.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-05

Roles and permissions are part of the code contract (route dependencies and Tool Gateway
policies check permission strings), so they are versioned with the schema, not seeded.
Matrix: architecture.md §15. Note: no role holds `actions:execute` - consequential actions
run only through a controlled tool with a verified approval (ADR-007), and ADMIN cannot
approve production actions (separation of duties).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = {
    "ENGINEER": "Investigates own services; read access to logs, code, incidents.",
    "SRE": "Investigates across services; may request remediation.",
    "INCIDENT_COMMANDER": "Runs incidents; approves or rejects consequential actions.",
    "MANAGER": "Reads KPIs, trends, postmortems and evaluation results.",
    "ADMIN": "Manages policies and roles; cannot approve production actions.",
}

PERMISSIONS = {
    "incidents:read": "Read incidents, timelines, evidence and reports",
    "incidents:write": "Create and update incidents",
    "investigations:run": "Start or re-run an AI investigation",
    "logs:read": "Query application logs through the Tool Gateway",
    "metrics:read": "Query metrics through the Tool Gateway",
    "code:read": "Read repositories, commits and pull requests",
    "deploys:read": "Read deployment history and config diffs",
    "docs:read": "Search engineering documents (still filtered by document groups)",
    "runbooks:read": "Read runbooks",
    "feedback:write": "Rate AI findings and answers",
    "actions:request": "Propose a consequential remediation action",
    "actions:approve": "Approve or reject a proposed consequential action",
    "audit:read_own": "Read audit events for own actions",
    "audit:read_incident": "Read the audit trail of incidents one is involved in",
    "audit:read_team": "Read audit events of own team",
    "audit:read_all": "Read all audit events",
    "kpis:read": "Read business KPI dashboards",
    "eval:read": "Read AI evaluation results",
    "policies:manage": "Manage Tool Gateway and retrieval policies",
    "roles:manage": "Assign roles and groups to users",
}

_ENGINEER = [
    "incidents:read",
    "investigations:run",
    "logs:read",
    "metrics:read",
    "code:read",
    "deploys:read",
    "docs:read",
    "runbooks:read",
    "feedback:write",
    "audit:read_own",
]
ROLE_PERMISSIONS = {
    "ENGINEER": _ENGINEER,
    "SRE": [*_ENGINEER, "actions:request"],
    "INCIDENT_COMMANDER": [
        *_ENGINEER,
        "actions:request",
        "actions:approve",
        "incidents:write",
        "audit:read_incident",
    ],
    "MANAGER": ["incidents:read", "docs:read", "kpis:read", "eval:read", "audit:read_team"],
    "ADMIN": [
        "incidents:read",
        "kpis:read",
        "eval:read",
        "audit:read_all",
        "policies:manage",
        "roles:manage",
    ],
}

_roles = sa.table("roles", sa.column("name"), sa.column("description"), schema="identity")
_perms = sa.table("permissions", sa.column("name"), sa.column("description"), schema="identity")
_rp = sa.table(
    "role_permissions", sa.column("role_name"), sa.column("permission_name"), schema="identity"
)


def upgrade() -> None:
    op.bulk_insert(_roles, [{"name": k, "description": v} for k, v in ROLES.items()])
    op.bulk_insert(_perms, [{"name": k, "description": v} for k, v in PERMISSIONS.items()])
    op.bulk_insert(
        _rp,
        [
            {"role_name": role, "permission_name": perm}
            for role, perms in ROLE_PERMISSIONS.items()
            for perm in perms
        ],
    )


def downgrade() -> None:
    # Downgrades must work WITH data present: role assignments reference these roles.
    op.execute(
        "DELETE FROM identity.user_roles WHERE role_name IN ("
        + ", ".join(f"'{r}'" for r in ROLES)
        + ")"
    )
    op.execute("DELETE FROM identity.role_permissions")
    op.execute(
        "DELETE FROM identity.permissions WHERE name IN ("
        + ", ".join(f"'{p}'" for p in PERMISSIONS)
        + ")"
    )
    op.execute(
        "DELETE FROM identity.roles WHERE name IN (" + ", ".join(f"'{r}'" for r in ROLES) + ")"
    )
