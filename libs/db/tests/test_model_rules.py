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
