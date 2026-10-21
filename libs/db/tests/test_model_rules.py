"""Static rules on the ORM metadata (no database needed)."""

from aeoi_db.base import SCHEMA_OWNERS
from aeoi_db.models import metadata


def test_every_table_is_in_an_owned_schema() -> None:
    for table in metadata.tables.values():
        assert table.schema in SCHEMA_OWNERS, table.fullname


def test_every_owned_schema_has_tables() -> None:
    used = {t.schema for t in metadata.tables.values()}
    assert used == set(SCHEMA_OWNERS)


def test_no_foreign_key_crosses_a_schema_boundary() -> None:
    """Ownership rule: a service references another service's data by id, never by FK."""
    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.schema == table.schema, (
                f"{table.fullname} -> {fk.target_fullname}"
            )


def test_every_table_has_a_primary_key() -> None:
    for table in metadata.tables.values():
        assert table.primary_key.columns, table.fullname


def test_status_like_columns_are_constrained() -> None:
    """Every status/kind/severity text column must have a CHECK constraint."""
    for table in metadata.tables.values():
        checks = " ".join(str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext"))
        for col in table.columns:
            if col.name in {
                "status",
                "severity",
                "kind",
                "decision",
                "side_effect",
                "level",
                "source",
            }:
                assert col.name in checks, f"{table.fullname}.{col.name} has no CHECK"


def test_no_migration_downgrade_drops_a_role() -> None:
    """Roles belong to the whole Postgres SERVER; the dev DB beside a test DB still grants them,
    so DROP ROLE in a downgrade fails and leaves the database half-migrated. Learned twice:
    0024 (Phase 16, on the Mac) and 0026 (Phase 23a, CI red for 29 integration tests)."""
    import ast
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    offenders = []
    for f in sorted(versions.glob("*.py")):
        tree = ast.parse(f.read_text())
        for fn in (
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
        ):
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "DROP ROLE" in node.value.upper()
                ):
                    offenders.append(f.name)
                if isinstance(node, ast.JoinedStr) and any(
                    isinstance(v, ast.Constant) and "DROP ROLE" in str(v.value).upper()
                    for v in node.values
                ):
                    offenders.append(f.name)
    assert not offenders, f"revoke grants instead of dropping the role: {sorted(set(offenders))}"
