"""Rules checked against the REAL migrated schema (catalog queries)."""

import psycopg
import pytest

pytestmark = pytest.mark.integration

OWNED = ("identity", "incident", "orchestrator", "rag", "devdata", "tools", "llm", "audit", "eval")


def test_every_explicit_index_is_documented(db: psycopg.Connection) -> None:
    """Spec: 'explain every important index'. The explanation lives in COMMENT ON INDEX."""
    rows = db.execute(
        """
        SELECT n.nspname || '.' || ci.relname
        FROM pg_index i
        JOIN pg_class ci ON ci.oid = i.indexrelid
        JOIN pg_class ct ON ct.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = ci.relnamespace
        WHERE n.nspname = ANY(%s)
          AND NOT ct.relispartition                              -- partition children inherit
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
          AND obj_description(ci.oid, 'pg_class') IS NULL
        """,
        (list(OWNED),),
    ).fetchall()
    assert not rows, f"indexes without COMMENT: {[r[0] for r in rows]}"


def test_every_foreign_key_has_a_supporting_index(db: psycopg.Connection) -> None:
    """Unindexed FKs make deletes/joins on the parent scan the whole child table."""
    rows = db.execute(
        """
        SELECT c.conrelid::regclass::text, c.conname
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE c.contype = 'f' AND n.nspname = ANY(%s)
          AND NOT EXISTS (
            SELECT 1 FROM pg_index i
            WHERE i.indrelid = c.conrelid
              AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] = c.conkey
          )
        """,
        (list(OWNED),),
    ).fetchall()
    assert not rows, f"FKs without a leading index: {rows}"


def test_no_foreign_keys_across_schemas(db: psycopg.Connection) -> None:
    rows = db.execute(
        """
        SELECT c.conname FROM pg_constraint c
        JOIN pg_class s ON s.oid = c.conrelid
        JOIN pg_class t ON t.oid = c.confrelid
        WHERE c.contype = 'f' AND s.relnamespace <> t.relnamespace
        """
    ).fetchall()
    assert not rows


def test_audit_is_partitioned_with_current_month(db: psycopg.Connection) -> None:
    parts = db.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'audit.audit_events'::regclass"
    ).fetchall()
    names = {p[0] for p in parts}
    current = db.execute('SELECT to_char(now(), \'"audit_events_y"YYYY"m"MM\')').fetchone()
    assert current and current[0] in names
    assert "audit_events_default" in names
