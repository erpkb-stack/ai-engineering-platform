"""Role -> permission matrix, as code (architecture.md §15).

The database holds the same matrix (migrations 0012 + 0013) for admin UIs and audits.
`tests/integration/security/test_rbac_matches_db.py` fails if the two ever differ, so
services can authorise locally from token roles without a DB round trip per request.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class Role(StrEnum):
    ENGINEER = "ENGINEER"
    SRE = "SRE"
    INCIDENT_COMMANDER = "INCIDENT_COMMANDER"
    MANAGER = "MANAGER"
    ADMIN = "ADMIN"


class Perm(StrEnum):
    INCIDENTS_READ = "incidents:read"
    INCIDENTS_CREATE = "incidents:create"
    INCIDENTS_WRITE = "incidents:write"
    INVESTIGATIONS_RUN = "investigations:run"
    LOGS_READ = "logs:read"
    METRICS_READ = "metrics:read"
    CODE_READ = "code:read"
    DEPLOYS_READ = "deploys:read"
    DOCS_READ = "docs:read"
    RUNBOOKS_READ = "runbooks:read"
    FEEDBACK_WRITE = "feedback:write"
    ACTIONS_REQUEST = "actions:request"
    ACTIONS_APPROVE = "actions:approve"
    AUDIT_READ_OWN = "audit:read_own"
    AUDIT_READ_INCIDENT = "audit:read_incident"
    AUDIT_READ_TEAM = "audit:read_team"
    AUDIT_READ_ALL = "audit:read_all"
    KPIS_READ = "kpis:read"
    EVAL_READ = "eval:read"
    POLICIES_MANAGE = "policies:manage"
    ROLES_MANAGE = "roles:manage"


_ENGINEER = frozenset(
    {
        Perm.INCIDENTS_READ,
        Perm.INCIDENTS_CREATE,
        Perm.INVESTIGATIONS_RUN,
        Perm.LOGS_READ,
        Perm.METRICS_READ,
        Perm.CODE_READ,
        Perm.DEPLOYS_READ,
        Perm.DOCS_READ,
        Perm.RUNBOOKS_READ,
        Perm.FEEDBACK_WRITE,
        Perm.AUDIT_READ_OWN,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Perm]] = {
    Role.ENGINEER: _ENGINEER,
    Role.SRE: _ENGINEER | {Perm.ACTIONS_REQUEST},
    Role.INCIDENT_COMMANDER: _ENGINEER
    | {Perm.ACTIONS_REQUEST, Perm.ACTIONS_APPROVE, Perm.INCIDENTS_WRITE, Perm.AUDIT_READ_INCIDENT},
    Role.MANAGER: frozenset(
        {Perm.INCIDENTS_READ, Perm.DOCS_READ, Perm.KPIS_READ, Perm.EVAL_READ, Perm.AUDIT_READ_TEAM}
    ),
    # Separation of duties: ADMIN manages policy but cannot approve production actions.
    Role.ADMIN: frozenset(
        {
            Perm.INCIDENTS_READ,
            Perm.KPIS_READ,
            Perm.EVAL_READ,
            Perm.AUDIT_READ_ALL,
            Perm.POLICIES_MANAGE,
            Perm.ROLES_MANAGE,
        }
    ),
}


def permissions_for(roles: list[str] | tuple[str, ...] | frozenset[str]) -> frozenset[Perm]:
    """Union of permissions of known roles. Unknown role names grant nothing."""
    perms: set[Perm] = set()
    for name in roles:
        try:
            perms |= ROLE_PERMISSIONS[Role(name)]
        except ValueError:
            continue
    return frozenset(perms)


# Who may READ stored evidence of each kind (ADR-020). Reading the incident is not enough:
# a MANAGER may read incidents but not raw log lines. Unknown kind -> nobody (default deny).
EVIDENCE_KIND_PERMS: dict[str, Perm] = {
    "LOG": Perm.LOGS_READ,
    "TRACE": Perm.LOGS_READ,
    "METRIC": Perm.METRICS_READ,
    "DEPLOY": Perm.DEPLOYS_READ,
    "CONFIG": Perm.DEPLOYS_READ,
    "COMMIT": Perm.CODE_READ,
    "PR": Perm.CODE_READ,
    # pointers only (no text) - the text is re-read through rag with the reader's own groups
    "DOC": Perm.DOCS_READ,
    "RUNBOOK": Perm.RUNBOOKS_READ,
    "INCIDENT": Perm.INCIDENTS_READ,
    "ALERT": Perm.INCIDENTS_READ,
    "CATALOG": Perm.INCIDENTS_READ,
}


def may_read_evidence(has: Callable[[Perm], bool], kind: str) -> bool:
    """`has` = Principal.has. Unknown kinds are never readable."""
    needed = EVIDENCE_KIND_PERMS.get(kind)
    return needed is not None and has(needed)
