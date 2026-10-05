"""Alembic environment for all AEOI schemas (ADR-013: one migration stream).

- Owned schemas only (aeoi_db.base.SCHEMA_OWNERS); `catalog` is Flyway's (Java, Phase 20).
- Monthly audit partitions are created by audit.ensure_partitions(), so autogenerate ignores them.
- `-x schema=<name>` limits autogenerate to one schema (one reviewable revision per schema).
"""

from __future__ import annotations

import re
from typing import Any

from alembic import context
from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine, pool

from aeoi_db.base import SCHEMA_OWNERS
from aeoi_db.config import database_url
from aeoi_db.models import metadata

config = context.config
x_args = context.get_x_argument(as_dictionary=True)
ONLY_SCHEMA = x_args.get("schema")
_PARTITION = re.compile(r"^audit_events_(y\d{4}m\d{2}|default)$")


def include_name(name: str | None, type_: str, parent_names: dict[str, Any]) -> bool:
    if type_ == "schema":
        return name in SCHEMA_OWNERS and (ONLY_SCHEMA is None or name == ONLY_SCHEMA)
    return True


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    if type_ == "table" and name and _PARTITION.match(name):
        return False
    if ONLY_SCHEMA and type_ == "table":
        return bool(getattr(obj, "schema", None) == ONLY_SCHEMA)
    return True


def _url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    return override or database_url().render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # Teach reflection about pgvector so `alembic check` compares vector columns correctly.
        connection.dialect.ischema_names["vector"] = Vector  # type: ignore[attr-defined]
        context.configure(
            connection=connection,
            target_metadata=metadata,
            include_schemas=True,
            include_name=include_name,
            include_object=include_object,
            version_table_schema="public",
            compare_type=True,
            compare_server_default=False,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
