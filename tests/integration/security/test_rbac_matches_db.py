"""The RBAC matrix in code (libs/security) must equal the one in the database."""

import psycopg
import pytest

from aeoi_security.rbac import ROLE_PERMISSIONS, Perm

pytestmark = pytest.mark.integration


def test_code_and_database_rbac_are_identical(db: psycopg.Connection) -> None:
    rows = db.execute("SELECT role_name, permission_name FROM identity.role_permissions").fetchall()
    in_db: dict[str, set[str]] = {}
    for role, perm in rows:
        in_db.setdefault(role, set()).add(perm)
    in_code = {role.value: {p.value for p in perms} for role, perms in ROLE_PERMISSIONS.items()}
    assert in_db == in_code


def test_every_permission_exists_in_db(db: psycopg.Connection) -> None:
    names = {r[0] for r in db.execute("SELECT name FROM identity.permissions").fetchall()}
    assert {p.value for p in Perm} == names
