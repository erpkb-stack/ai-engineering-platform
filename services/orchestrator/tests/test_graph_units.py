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


# ------------------------------------------------------------------ Phase 10 outcome (ADR-020)
def _r(agent: str, status: str = "SUCCEEDED", error: str | None = None) -> dict[str, object]:
    return {"agent": agent, "status": status, "error": error}


def test_outcome_complete_partial_failed() -> None:
    from aeoi_orchestrator.graph import outcome

    assert outcome([_r("log_analysis"), _r("metrics")], None) == ("COMPLETE", None)
    status, error = outcome([_r("log_analysis"), _r("knowledge", "FAILED", "rag down")], None)
    assert status == "PARTIAL" and error and "knowledge: rag down" in error
    assert outcome([_r("metrics", "FAILED", "x")], None)[0] == "FAILED"
    assert outcome([], None) == ("FAILED", "no tasks ran")
    # evidence that was not stored: FAILED (repost-evidence repairs it), even if all agents ran
    status, error = outcome([_r("metrics")], "metrics: evidence was not stored")
    assert status == "FAILED" and error and "evidence was not stored" in error


def test_outcome_names_incomplete_evidence_of_a_succeeded_task() -> None:
    """Phase 25a regression (eval run 2026-10-22): metrics SUCCEEDED with 7 of 9 queries
    rate-limited and the run said COMPLETE/INCONCLUSIVE as if the signal was absent."""
    from aeoi_orchestrator.graph import outcome

    lost = {**_r("metrics"), "degraded": "failed: a/cpu_util (rate_limited)"}
    status, error = outcome([_r("log_analysis"), lost], None)
    assert status == "PARTIAL" and error and "metrics: evidence incomplete" in error
    assert "rate_limited" in error
    # a deployment task that could not read deploys is the same; labels-by-rule is not
    gone = {**_r("deployment"), "degraded": "no data: get_deployment a (timeout)"}
    assert outcome([gone], None)[0] == "PARTIAL"
    labels = {**_r("log_analysis"), "degraded": "labels by rule for 2 cluster(s)"}
    assert outcome([labels, _r("metrics")], None) == ("COMPLETE", None)


def test_a_postmortem_problem_is_partial_and_named_but_never_hides_a_worse_status() -> None:
    """Phase 12 (ADR-022): written by code only (model down), failed, or not stored = PARTIAL
    with the reason; skipped = no change; INCONCLUSIVE/FAILED stay what they are."""
    from aeoi_orchestrator.graph import report_outcome

    ok = {"task": {"status": "SUCCEEDED"}}
    assert report_outcome("COMPLETE", None, ok) == ("COMPLETE", None)
    assert report_outcome("COMPLETE", None, {"skipped": True}) == ("COMPLETE", None)
    s, e = report_outcome("COMPLETE", None, {**ok, "degraded": "narrative not written: 503"})
    assert s == "PARTIAL" and e == "postmortem: narrative not written: 503"
    s, e = report_outcome("COMPLETE", None, {**ok, "post_error": "postmortem was not stored"})
    assert s == "PARTIAL" and "not stored" in (e or "")
    s, e = report_outcome("COMPLETE", None, {"task": {"status": "FAILED", "error": "boom"}})
    assert s == "PARTIAL" and e == "postmortem: boom"
    s, e = report_outcome("INCONCLUSIVE", "no hypothesis", {**ok, "degraded": "x"})
    assert s == "INCONCLUSIVE" and e == "no hypothesis; postmortem: x"
    assert report_outcome("FAILED", "dead", {**ok, "degraded": "x"}) == ("FAILED", "dead")


def test_history_problems_are_partial_and_named_but_never_hide_a_worse_status() -> None:
    """Phase 13 (ADR-023): a failed search, code-only comparison or unstored pointers is a
    missing source like any other (ADR-020)."""
    from aeoi_orchestrator.graph import history_outcome

    ok = {"task": {"status": "SUCCEEDED"}, "similar": ["INC-1"]}
    assert history_outcome("COMPLETE", None, ok) == ("COMPLETE", None)
    assert history_outcome("COMPLETE", None, {"skipped": True}) == ("COMPLETE", None)
    s, e = history_outcome("COMPLETE", None, {"task": {"status": "FAILED", "error": "denied"}})
    assert s == "PARTIAL" and e == "historical_incident: denied"
    s, e = history_outcome("COMPLETE", None, {**ok, "degraded": "compared by code: 503"})
    assert s == "PARTIAL" and "compared by code" in (e or "")
    s, e = history_outcome("COMPLETE", None, {**ok, "post_error": "not stored"})
    assert s == "PARTIAL" and "not stored" in (e or "")
    s, e = history_outcome("INCONCLUSIVE", "no hypothesis", {**ok, "degraded": "x"})
    assert s == "INCONCLUSIVE" and e == "no hypothesis; historical_incident: x"
    assert history_outcome("FAILED", "dead", {**ok, "degraded": "x"}) == ("FAILED", "dead")


def test_code_problems_are_partial_and_named_but_never_hide_a_worse_status() -> None:
    """Phase 14 (ADR-024): code that could not be checked is a missing source, named."""
    from aeoi_orchestrator.graph import code_outcome

    ok = {"task": {"status": "SUCCEEDED"}, "findings": 1}
    assert code_outcome("COMPLETE", None, ok) == ("COMPLETE", None)
    assert code_outcome("COMPLETE", None, {"skipped": True}) == ("COMPLETE", None)
    assert code_outcome("COMPLETE", None, {}) == ("COMPLETE", None)
    s, e = code_outcome("COMPLETE", None, {"task": {"status": "FAILED", "error": "x.Y: denied"}})
    assert s == "PARTIAL" and e == "code_intelligence: FAILED (see the trace)"
    s, e = code_outcome("COMPLETE", None, {**ok, "degraded": True})
    # readable by every incident reader: names the problem, never a symbol (review)
    assert s == "PARTIAL" and e == "code_intelligence: code only partly checked (see the trace)"
    s, e = code_outcome("COMPLETE", None, {**ok, "post_error": "not stored"})
    assert s == "PARTIAL" and "not stored" in (e or "")
    s, e = code_outcome("INCONCLUSIVE", "no hypothesis", {**ok, "degraded": True})
    assert s == "INCONCLUSIVE" and e == (
        "no hypothesis; code_intelligence: code only partly checked (see the trace)"
    )
    assert code_outcome("FAILED", "dead", {**ok, "degraded": True}) == ("FAILED", "dead")
