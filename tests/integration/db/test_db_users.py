"""`make db-users` (aeoi_db.users): a login whose group comes from OUTSIDE Alembic (Phase 20:
svc_catalog from Flyway) is skipped with the fix, not a crash; created once the group exists.
Throwaway role names and a temp secrets root: never the real logins or secrets/."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from aeoi_db import users
from aeoi_db.config import libpq_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as c:
        yield c


def test_a_missing_optional_group_is_skipped_then_created(
    conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = secrets.token_hex(3)
    group, login = f"svc_t20_{tag}", f"t20_login_{tag}"
    monkeypatch.setattr(users, "OPTIONAL_GROUPS", {group: "make catalog-import"})
    try:
        out = users.ensure_all(conn, tmp_path, {login: group})
        assert out[login].startswith("skipped:") and "make catalog-import" in out[login]
        assert (
            conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)).fetchone() is None
        )
        assert not (tmp_path / "secrets" / f"{login}_password.txt").exists()

        conn.execute(f"CREATE ROLE {group} NOLOGIN")  # what Flyway's V1 does
        assert users.ensure_all(conn, tmp_path, {login: group}) == {login: "ok"}
        member = conn.execute(
            "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = %s AND u.rolname = %s",
            (group, login),
        ).fetchone()
        assert member is not None
        pw = tmp_path / "secrets" / f"{login}_password.txt"
        assert pw.stat().st_mode & 0o777 == 0o600
    finally:
        conn.execute(f"DROP ROLE IF EXISTS {login}")
        conn.execute(f"DROP ROLE IF EXISTS {group}")


def test_a_missing_required_group_still_fails_loudly(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Only the listed optional groups are skipped: a typo'd group must not pass silently."""
    login = f"t20_x_{secrets.token_hex(3)}"
    try:
        with pytest.raises(psycopg.errors.UndefinedObject):
            users.ensure_all(conn, tmp_path, {login: "svc_no_such_group"})
    finally:  # the login is created before the GRANT fails
        conn.execute(f"DROP ROLE IF EXISTS {login}")
