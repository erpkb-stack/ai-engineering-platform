"""Least privilege per service role (schema ownership) and for the read-only AI role."""

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.integration


def as_role(db: psycopg.Connection, role: str, sql: str) -> None:
    with db.transaction():
        db.execute(f"SET LOCAL ROLE {role}")
        db.execute(sql)


@pytest.mark.parametrize(
    ("role", "sql"),
    [
        ("aeoi_readonly", "SELECT count(*) FROM incident.incidents"),
        ("aeoi_readonly", "SELECT count(*) FROM devdata.log_events"),
        ("svc_rag", "SELECT count(*) FROM rag.documents"),
        ("svc_incident", "SELECT count(*) FROM incident.approvals"),
        ("svc_audit", "SELECT count(*) FROM audit.audit_events"),
    ],
)
def test_allowed(db: psycopg.Connection, role: str, sql: str) -> None:
    as_role(db, role, sql)


@pytest.mark.parametrize(
    ("role", "sql"),
    [
        ("aeoi_readonly", "SELECT count(*) FROM identity.users"),  # people data: not for AI tools
        ("aeoi_readonly", "SELECT count(*) FROM audit.audit_events"),
        ("aeoi_readonly", "DELETE FROM incident.incidents"),
        ("svc_rag", "SELECT count(*) FROM incident.incidents"),  # other service's schema
        ("svc_tool_gateway", "UPDATE incident.approvals SET reason = 'x'"),
        ("svc_audit", "UPDATE audit.audit_events SET outcome = 'DENIED'"),  # INSERT/SELECT only
        ("svc_incident", "INSERT INTO audit.audit_events (actor) VALUES ('user:x')"),
    ],
)
def test_denied(db: psycopg.Connection, role: str, sql: str) -> None:
    with pytest.raises((errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)):
        as_role(db, role, sql)
