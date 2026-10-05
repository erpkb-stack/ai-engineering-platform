"""Integration fixtures: a throwaway database per test session on the local Postgres.

Uses the running compose Postgres (`make up`) - same image as production-like runs, and
much faster than starting a Testcontainer per run on an Intel Mac. Each session creates
`aeoi_test_<random>`, migrates it to head, and drops it at the end.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config

from aeoi_db.config import database_url, find_repo_root, libpq_dsn


def alembic_config(sqlalchemy_url: str) -> Config:
    root = find_repo_root()
    cfg = Config(str(root / "libs" / "db" / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "libs" / "db" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def test_db_name() -> Iterator[str]:
    name = f"aeoi_test_{secrets.token_hex(4)}"
    try:
        admin = psycopg.connect(libpq_dsn(database="postgres"), autocommit=True)
    except (psycopg.OperationalError, FileNotFoundError) as exc:
        pytest.skip(f"Postgres not reachable ({exc}); run `make up` first")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield name
    with psycopg.connect(libpq_dsn(database="postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migrated_db(test_db_name: str) -> str:
    """Name of a database migrated to head (once per session)."""
    from alembic import command

    url = database_url(database=test_db_name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    return test_db_name


@pytest.fixture
def db(migrated_db: str) -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back after each test."""
    with psycopg.connect(libpq_dsn(database=migrated_db)) as conn:
        yield conn
        conn.rollback()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return find_repo_root()
