"""Resolve the database URL without ever hard-coding a password.

Order: AEOI_DATABASE_URL (CI / production) > local dev default built from
AEOI_PG_PORT + secrets/postgres_password.txt (created by `make setup`).
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import URL, make_url

DEFAULT_PG_PORT = 5433


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up until the uv workspace root (pyproject with [tool.uv.workspace])."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and "[tool.uv.workspace]" in pyproject.read_text():
            return candidate
    raise FileNotFoundError(
        "could not find the AEOI repo root (pyproject with [tool.uv.workspace])"
    )


def _password_file() -> Path:
    explicit = os.environ.get("AEOI_PG_PASSWORD_FILE")
    if explicit:
        return Path(explicit)
    return find_repo_root() / "secrets" / "postgres_password.txt"


def database_url(*, database: str | None = None, driver: str = "postgresql+psycopg") -> URL:
    """SQLAlchemy URL for the AEOI database (or another database on the same server)."""
    raw = os.environ.get("AEOI_DATABASE_URL")
    if raw:
        url = make_url(raw).set(drivername=driver)
        return url.set(database=database) if database else url
    pw_file = _password_file()
    if not pw_file.is_file():
        raise FileNotFoundError(f"{pw_file} not found - run `make setup`, or set AEOI_DATABASE_URL")
    return URL.create(
        drivername=driver,
        username=os.environ.get("AEOI_PG_USER", "aeoi"),
        password=pw_file.read_text().strip(),
        host=os.environ.get("AEOI_PG_HOST", "localhost"),
        port=int(os.environ.get("AEOI_PG_PORT", DEFAULT_PG_PORT)),
        database=database or os.environ.get("AEOI_PG_DATABASE", "aeoi"),
    )


def libpq_dsn(*, database: str | None = None) -> str:
    """Plain postgresql:// DSN for psycopg / psql (no SQLAlchemy driver suffix)."""
    return database_url(database=database, driver="postgresql").render_as_string(
        hide_password=False
    )


def service_database_url(
    *,
    user: str,
    password_file: Path,
    database: str | None = None,
    driver: str = "postgresql+psycopg",
) -> URL:
    """URL for a least-privilege service LOGIN user (created by `make db-users`)."""
    if os.environ.get("AEOI_DATABASE_URL"):
        # CI / production: the platform injects a full URL (already the right user).
        return database_url(database=database, driver=driver)
    if not password_file.is_file():
        raise FileNotFoundError(f"{password_file} not found - run `make db-users`")
    return URL.create(
        drivername=driver,
        username=user,
        password=password_file.read_text().strip(),
        host=os.environ.get("AEOI_PG_HOST", "localhost"),
        port=int(os.environ.get("AEOI_PG_PORT", DEFAULT_PG_PORT)),
        database=database or os.environ.get("AEOI_PG_DATABASE", "aeoi"),
    )
