"""Phase 28a (ADR-040): the failure-experiment rules are fixed BEFORE a run - these tests pin
them, so a FAIL cannot be turned into a PASS by quietly editing a threshold."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/chaos"))
from chaoslib import (
    EXPERIMENTS,
    KCTL,
    RECOVER_WITHIN_S,
    REQUEST_TIMEOUT_S,
    judge,
    restart_delta,
)

pytestmark = pytest.mark.unit

FAULT, BACK = 30.0, 90.0


def reads(*spans: tuple[float, float, str, float]) -> list[tuple[float, str, float]]:
    """(from, to, outcome, latency) -> one read every 0.5 s."""
    rows = []
    for a, b, o, lat in spans:
        t = a
        while t < b:
            rows.append((t, o, lat))
            t += 0.5
    return rows


def healthy_run(**kw: object) -> dict:
    rows = reads((0, FAULT, "200", 0.05), (FAULT, BACK + 10, "ConnectionRefusedError", 0.01),
                 (BACK + 10, 200, "200", 0.05))  # fmt: skip
    args = {"investigation": "PARTIAL", "restarts": {"api": 0, "incident-service": 0}} | kw
    return judge(rows, FAULT, BACK, 200, **args)  # type: ignore[arg-type]


def test_a_clean_outage_and_recovery_passes() -> None:
    v = healthy_run()
    assert v["pass"]
    assert v["rules"]["recovers"]["seconds_to_first_ok"] == 10.0
    assert v["during_fault"]["ConnectionRefusedError"] > 0  # what the caller SAW is recorded


def test_a_request_that_waits_for_the_client_timeout_is_a_hang() -> None:
    rows = reads((0, FAULT, "200", 0.05), (FAULT, FAULT + 1, "TimeoutError", REQUEST_TIMEOUT_S),
                 (FAULT + 1, 200, "200", 0.05))  # fmt: skip
    v = judge(rows, FAULT, BACK, 200, "COMPLETE", {})
    assert not v["rules"]["no_hang"]["pass"] and not v["pass"]


def test_slow_recovery_fails() -> None:
    late = BACK + RECOVER_WITHIN_S + 5
    rows = reads((0, FAULT, "200", 0.05), (FAULT, late, "503", 0.02), (late, 300, "200", 0.05))
    v = judge(rows, FAULT, BACK, 300, "COMPLETE", {})
    assert not v["rules"]["recovers"]["pass"]


def test_errors_in_the_last_30_seconds_fail_recovery_even_after_a_first_ok() -> None:
    rows = reads((0, FAULT, "200", 0.05), (FAULT, BACK + 5, "503", 0.02),
                 (BACK + 5, 180, "200", 0.05), (180, 200, "502", 0.02))  # fmt: skip
    assert not judge(rows, FAULT, BACK, 200, "COMPLETE", {})["rules"]["recovers"]["pass"]


def test_a_restart_fails_and_no_kubectl_means_not_measured_not_passed() -> None:
    assert not healthy_run(restarts={"api": 1})["pass"]
    v = healthy_run(restarts=None)
    assert v["rules"]["no_restarts"]["pass"] is None and v["pass"]


def test_an_investigation_stuck_running_fails() -> None:
    assert not healthy_run(investigation="RUNNING")["pass"]
    assert healthy_run(investigation="FAILED")["pass"]  # a NAMED end is the rule, not success


def test_a_run_that_ended_before_the_fault_is_not_exercised() -> None:
    v = healthy_run(investigation=None)
    assert v["rules"]["investigation_named"]["pass"] is None


def test_kafka_rules_writes_and_outbox() -> None:
    assert healthy_run(writes=[201, 201], backlog_after=0)["pass"]
    assert not healthy_run(writes=[201, 503], backlog_after=0)["pass"]
    assert not healthy_run(writes=[], backlog_after=0)[
        "pass"
    ]  # no write during the fault = unproven
    assert not healthy_run(writes=[201], backlog_after=3)["pass"]


def test_restart_delta_ignores_the_app_whose_pod_was_deleted() -> None:
    before = {"orchestrator": 2, "api": 1, "agents": 0}
    after = {"orchestrator": 0, "api": 1, "agents": 1}
    assert restart_delta(before, after, "orchestrator") == {"api": 0, "agents": 1}


def test_experiments_are_the_announced_four_and_pin_the_kind_context() -> None:
    assert set(EXPERIMENTS) == {"pg", "kafka", "orchestrator", "agents"}
    assert KCTL[:3] == ["kubectl", "--context", "kind-aeoi"]
    for name in ("orchestrator", "agents"):
        e = EXPERIMENTS[name]
        assert e.undo is None and e.fault[: len(KCTL)] == tuple(KCTL)
        assert f"app.kubernetes.io/name={name}" in e.fault
    assert EXPERIMENTS["kafka"].writes and not EXPERIMENTS["pg"].writes
    for name in ("pg", "kafka"):
        assert EXPERIMENTS[name].fault[:3] == ("docker", "compose", "stop")
    assert all(e.expect for e in EXPERIMENTS.values())  # the prediction is written before the run
