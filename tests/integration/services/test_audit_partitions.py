"""Phase 30 (architecture review): the audit service keeps its monthly partitions itself.

On the Mac (2026-10-30) the partitions ended at 2026-12: migration 0011 created them once and
nothing ran `audit.ensure_partitions` again. From 2027-01 every row would land in the default
partition, and creating that month afterwards fails - for every later month too.
Real Postgres, as the audit LOGIN (svc_audit: no DDL right on the table - the function is
SECURITY DEFINER, migration 0030)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from aeoi_audit.main import ensure_partitions
from aeoi_db.config import database_url, libpq_dsn
from aeoi_db.users import ensure_login

pytestmark = pytest.mark.integration


def _month(offset: int) -> datetime:
    now = datetime.now(UTC)
    y, m = divmod(now.month - 1 + offset, 12)
    return datetime(now.year + y, m + 1, 1, tzinfo=UTC)


def _part(at: datetime) -> str:
    return f"audit_events_y{at:%Y}m{at:%m}"


def _parts(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as c:
        rows = c.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'audit.audit_events'::regclass"
        ).fetchall()
    return {r[0] for r in rows}


@pytest.fixture(scope="module")
def audit_login_url(migrated_db: str) -> str:
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        ensure_login(conn, "audit_svc_t30", "svc_audit", password)
    return (
        database_url(database=migrated_db)
        .set(username="audit_svc_t30", password=password)
        .render_as_string(hide_password=False)
    )


async def test_the_audit_login_creates_the_months_ahead(
    migrated_db: str, audit_login_url: str
) -> None:
    engine = create_async_engine(audit_login_url)
    try:
        assert await ensure_partitions(engine, 5) is not None  # EXECUTE granted, runs as owner
        assert await ensure_partitions(engine, 5) == 0  # idempotent
    finally:
        await engine.dispose()
    have = _parts(libpq_dsn(database=migrated_db))
    assert {_part(_month(k)) for k in range(0, 6)} <= have


async def test_a_month_already_in_the_default_partition_is_skipped_not_fatal(
    migrated_db: str, audit_login_url: str
) -> None:
    """v1 RAISED here - and stopped creating every later month too."""
    dsn = libpq_dsn(database=migrated_db)
    blocked = _month(9)
    assert _part(blocked) not in _parts(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:  # a row with no partition -> default
        c.execute(
            "INSERT INTO audit.audit_events (id, occurred_at, actor, action, resource_type,"
            " outcome) VALUES (%s, %s, 'service:t30', 'test.default_row', 'test', 'SUCCESS')",
            (uuid4(), blocked.replace(day=15)),
        )
    engine = create_async_engine(audit_login_url)
    try:
        created = await ensure_partitions(engine, 10)
    finally:
        await engine.dispose()
    assert created is not None  # no exception: logged as a WARNING by Postgres
    have = _parts(dsn)
    assert _part(blocked) not in have  # skipped, the row stays where it is
    assert _part(_month(10)) in have  # the month AFTER it is still created


def test_execute_is_not_granted_to_public(migrated_db: str) -> None:
    with psycopg.connect(libpq_dsn(database=migrated_db)) as c:
        row = c.execute(
            "SELECT has_function_privilege('public', 'audit.ensure_partitions(int)', 'EXECUTE'),"
            " has_function_privilege('svc_audit', 'audit.ensure_partitions(int)', 'EXECUTE')"
        ).fetchone()
    assert row == (False, True)
