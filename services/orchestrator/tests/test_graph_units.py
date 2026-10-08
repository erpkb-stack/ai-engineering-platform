"""Orchestrator invariants that need no IO (ADR-019)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres.base import MIGRATIONS
from pydantic import ValidationError

from aeoi_db.models.orchestrator import CHECKPOINT_MIGRATIONS
from aeoi_models.api.investigations import StartInvestigation
from aeoi_orchestrator.graph import task_id_for

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "libs/db/alembic/versions/0018_delegation_and_checkpoints.py"
)


def test_checkpoint_ddl_mirrors_every_library_migration() -> None:
    """A langgraph-checkpoint-postgres upgrade that adds a migration must fail HERE, not at
    runtime: mirror it in a new Alembic revision, then bump the constant."""
    spec = importlib.util.spec_from_file_location("m0018", MIGRATION)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert len(MIGRATIONS) == CHECKPOINT_MIGRATIONS == m.CHECKPOINT_MIGRATIONS


def test_task_ids_are_deterministic_per_investigation_and_agent() -> None:
    """A re-run of `plan` after a crash must produce the SAME task ids (idempotent insert)."""
    a = task_id_for("inv-1", "log_analysis")
    assert a == task_id_for("inv-1", "log_analysis")
    assert a != task_id_for("inv-2", "log_analysis")
    assert a != task_id_for("inv-1", "metrics")
    assert a != task_id_for("inv-1", "log_analysis", 2)


def test_start_window_needs_both_ends_with_timezones() -> None:
    StartInvestigation(incident="INC-10001")
    with pytest.raises(ValidationError):
        StartInvestigation(incident="INC-1", start="2026-10-02T09:00:00Z")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        StartInvestigation(
            incident="INC-1",
            start="2026-10-02T09:00:00",
            end="2026-10-02T10:00:00",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        StartInvestigation(incident="INC-1", extra="x")  # type: ignore[call-arg]
