"""Phase 29 (ADR-043): the demo's checks. A step that needs a real model is n/a on fakes and
never counts as a pass; a rehearsal can never be reported as a counted pass."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/demo"))
import demolib as d

pytestmark = pytest.mark.unit


def task(agent: str, a: str, b: str) -> dict:
    return {"agent": agent, "started_at": f"2026-10-27T10:00:{a}+00:00",
            "finished_at": f"2026-10-27T10:00:{b}+00:00"}  # fmt: skip


def hyp(rank: int, statement: str, status: str = "PROPOSED", conf: str = "HIGH",
        verdict: str = "not_reviewed") -> dict:  # fmt: skip
    # the API's HypothesisRecord shape: the critic's verdict lives in `detail`
    return {"rank": rank, "statement": statement, "status": status, "confidence": conf,
            "detail": {"critic_verdict": verdict}}  # fmt: skip


def test_parallel_needs_all_four_evidence_agents_overlapping() -> None:
    together = [task(a, "01", "09") for a in d.EVIDENCE_AGENTS]
    assert d.parallel_agents({"tasks": together})[0] == d.PASS
    one_after = [*together[:3], task("knowledge", "10", "12")]
    assert d.parallel_agents({"tasks": one_after})[0] == d.FAIL
    assert d.parallel_agents({"tasks": together[:3]})[0] == d.FAIL


def test_evidence_kinds_and_injection() -> None:
    assert d.evidence_kinds([{"kind": k} for k in d.REQUIRED_KINDS])[0] == d.PASS
    assert d.evidence_kinds([{"kind": "LOG"}])[0] == d.FAIL
    assert d.injection([{"source_uri": "runbooks/x.md"}])[0] == d.PASS
    assert d.injection([{"source_uri": "adversarial/runbook-cache-flush.md"}])[0] == d.FAIL


def test_top_hypothesis_ignores_rejected_and_the_decoy_must_not_win() -> None:
    hyps = [
        hyp(1, "traffic surge", status="REJECTED"),
        hyp(2, "Deploy DEPLOY-4821 exhausted the pool"),
    ]
    assert d.top_hypothesis(hyps)[0] == d.PASS
    assert d.decoy(hyps)[0] == d.PASS  # no cart-cache candidate at all
    assert d.decoy([hyp(1, "cart-cache restart", conf="MEDIUM")])[0] == d.FAIL
    assert d.decoy([hyp(3, "cart-cache restart", status="REJECTED", conf="HIGH")])[0] == d.PASS


def test_real_model_steps_are_na_on_fakes_and_never_a_counted_pass() -> None:
    hyps = [hyp(1, "Deploy DEPLOY-4821", status="VALIDATED", verdict="supported")]
    assert d.critic_challenge(hyps, real=False)[0] == d.NA
    assert d.critic_challenge(hyps, real=True)[0] == d.PASS
    assert d.critic_challenge([hyp(1, "Deploy DEPLOY-4821")], real=True)[0] == d.FAIL
    steps = {"a": (d.PASS, ""), "b": (d.NA, "")}
    assert d.verdict(steps, real=False) == "REHEARSAL-OK"
    assert d.verdict(steps, real=True) == "FAIL"  # an n/a on real models is not a pass
    assert d.verdict({"a": (d.PASS, "")}, real=True) == "PASS"
    assert d.verdict({"a": (d.FAIL, "")}, real=False) == "FAIL"


def test_proposal_approval_and_record() -> None:
    aps = [{"id": "x", "action": "rollback_deployment", "source": "investigation", "status": "PENDING",
            "action_args": {"deploy_key": d.DEMO_DEPLOY}, "requested_by": "user:1"}]  # fmt: skip
    (st, _), aid = d.proposal(aps, real=True)
    assert st == d.PASS and aid == "x"
    assert d.proposal(aps, real=False)[0][0] == d.NA
    ok = ({"executed": True}, {"status": "EXECUTED"}, "MITIGATED")
    assert d.approval(403, *ok)[0] == d.PASS
    assert d.approval(200, *ok)[0] == d.FAIL  # an ENGINEER must never decide
    rec = {"decided_by": "u", "decided_at": "t", "reason": "r", "args_sha256": "ab" * 32,
           "tool_call_id": "c", "decider_role": "INCIDENT_COMMANDER"}  # fmt: skip
    assert d.decision_record(rec)[0] == d.PASS
    assert d.decision_record({**rec, "args_sha256": ""})[0] == d.FAIL


def test_a_crashing_check_is_a_fail_and_the_runner_goes_on() -> None:
    """Mac, first real-model run: a check read a key the API does not have and the runner died
    with a traceback after paying for the investigation."""
    st, why = d.safe(d.critic_challenge, [{"rank": 1, "status": "VALIDATED"}], True)
    assert st == d.PASS or st == d.FAIL  # never an exception
    st, why = d.safe(d.top_hypothesis, [{"no": "fields"}])
    assert st == d.FAIL and why.startswith("check crashed: KeyError")
