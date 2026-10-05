"""Create/refresh LOGIN users for services (idempotent). Run: make db-users

Each service connects as its own login user, which is a member of its schema role
(svc_<service>, migration 0001). So a bug or an injected query in one service cannot
touch another service's schema. Passwords live in secrets/<user>_password.txt (gitignored).
"""

from __future__ import annotations

import secrets
from pathlib import Path

import psycopg
from psycopg import sql

from aeoi_db.config import find_repo_root, libpq_dsn

# login user -> group role it gets (add one line per new service)
SERVICE_LOGINS: dict[str, str] = {
    "incident_svc": "svc_incident",
}


def password_file(user: str, root: Path | None = None) -> Path:
    return (root or find_repo_root()) / "secrets" / f"{user}_password.txt"


def ensure_password(user: str, root: Path | None = None) -> str:
    path = password_file(user, root)
    if not path.exists():
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(secrets.token_hex(24))
        path.chmod(0o600)
    return path.read_text().strip()


def ensure_login(conn: psycopg.Connection, user: str, group: str, password: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone()
    verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
    conn.execute(
        sql.SQL("{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT").format(
            verb, sql.Identifier(user), sql.Literal(password)
        )
    )
    conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(user)))
    conn.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(sql.Identifier(user))
    )


def main() -> int:
    with psycopg.connect(libpq_dsn(), autocommit=True) as conn:
        for user, group in SERVICE_LOGINS.items():
            ensure_login(conn, user, group, ensure_password(user))
            print(f"  login user {user:<16} -> member of {group}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
