"""The synthetic dataset loads into the real schema, twice (idempotent)."""

import psycopg
import pytest

from aeoi_db.config import libpq_dsn
from aeoi_synth.generate import Scale, generate
from aeoi_synth.load import load

pytestmark = pytest.mark.integration


def test_load_small_dataset_twice(migrated_db: str) -> None:
    ds = generate(42, Scale.small())
    dsn = libpq_dsn(database=migrated_db)
    first = load(ds, dsn)
    second = load(ds, dsn)
    assert first == second
    with psycopg.connect(dsn) as conn:
        for table, expected in first.items():
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row and row[0] == expected, table
        demo = conn.execute(
            "SELECT version FROM devdata.deployments WHERE deploy_key = 'DEPLOY-4821'"
        ).fetchone()
        assert demo == ("2026.10.02.4",)
