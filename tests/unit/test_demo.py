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


def _ans(outcome: str, key: str = "INC-10042") -> tuple[int, dict]:
    return 200, {"received": 1, "results": [{"fingerprint": "demo1", "outcome": outcome,
                 "incident_key": key, "severity": "SEV1",
                 "reason": "label severity=error -> SEV2; tier 1 -> raised to SEV1"}]}  # fmt: skip


def test_alert_intake_needs_one_incident_and_a_duplicate_for_the_resend() -> None:
    step, key = d.alert_intake(_ans("created"), _ans("duplicate"))
    assert step[0] == d.PASS and key == "INC-10042" and "tier 1" in step[1]
    # the re-send opened a SECOND incident: the dedupe is broken
    step, key = d.alert_intake(_ans("created"), _ans("created", "INC-10043"))
    assert step[0] == d.FAIL and key == "INC-10042"  # the run can still go on
    # unsigned / not configured / the api down: no incident, the run stops
    for bad in ((401, None), (503, None), (0, "ConnectionRefusedError")):
        step, key = d.alert_intake(bad, bad)
        assert step[0] == d.FAIL and key is None
    # a valid answer that created nothing (rejected) is a FAIL with the reason
    step, key = d.alert_intake(_ans("rejected"), _ans("rejected"))
    assert step[0] == d.FAIL and key is None


def _pm(removed: list[dict], dropped: int, numbers: int = 0, **over: object) -> dict:
    return {"removed": removed, "sentences_dropped": dropped, "numbers_rejected": numbers,
            "removed_kept": True, "removed_redacted": 0, **over}  # fmt: skip


def test_removed_shown_proves_the_mechanism_and_zero_is_an_honest_pass() -> None:
    gone = [
        {"section": "why", "reason": "unknown_number", "text": "About 40% of checkouts failed."}
    ]
    md = '# Postmortem\n## Why\nok\n## Removed by code (model text)\n- Why: "About 40% ..."'
    hyps = [{"detail": {"explanation_removed": "uncited"}}, {"detail": {}}]
    step = d.removed_shown(_pm(gone, 0, 1), md, hyps)
    assert step == (d.PASS, "1 removed (unknown_number), shown apart with reasons; "
                            "1 ranker explanation(s) removed")  # fmt: skip
    # owner decision: nothing removed is a PASS that says so (never planted)
    assert d.removed_shown(_pm([], 0), "# Postmortem", []) == (
        d.PASS,
        "0 removed from the postmortem this run; 0 ranker explanation(s) removed",
    )
    bad = {
        "counts disagree": d.removed_shown(_pm(gone, 2, 1), md, []),
        "not kept": d.removed_shown(_pm([], 1, removed_kept=False), md, []),
        "hidden from the SRE": d.removed_shown(_pm(gone, 0, 1, removed_redacted=1), md, []),
        "not in markdown": d.removed_shown(_pm(gone, 0, 1), "# Postmortem", []),
        "inside the report": d.removed_shown(
            _pm(gone, 0, 1), "About 40% of checkouts failed.\n## Removed by code\n", []),
        "no postmortem": d.removed_shown(None, "", []),
    }  # fmt: skip
    assert all(s[0] == d.FAIL for s in bad.values()), bad


def _edge(key: str, kind: str, stance: str = "SUPPORTS") -> dict:
    return {"evidence_key": key, "kind": kind, "stance": stance}


def test_evidence_graph_needs_a_supported_top_cause_and_no_dangling_node() -> None:
    top = {
        **hyp(1, "Deploy DEPLOY-4821 may have caused it"),
        "edges": [_edge("DEPLOY-1", "DEPLOY"), _edge("LOG-1", "LOG"), _edge("METRIC-1", "METRIC")],
    }
    pool = {
        **hyp(2, "pool saturation", conf="MEDIUM"),
        "edges": [_edge("METRIC-2", "METRIC"), _edge("DEPLOY-1", "DEPLOY", "CONTRADICTS")],
    }
    ev = [{"evidence_key": k} for k in ("DEPLOY-1", "LOG-1", "METRIC-1", "METRIC-2")]
    assert d.evidence_graph([top, pool], ev) == (
        d.PASS, "2 hypotheses, 4 evidence nodes, 5 links (1 contradicting); "
                "top supported by DEPLOY LOG METRIC")  # fmt: skip
    # a node the reader cannot open = a broken graph
    assert d.evidence_graph([top, pool], ev[:3])[0] == d.FAIL
    # the top cause without the deploy behind it, or with fewer than 3 kinds
    thin = {**top, "edges": top["edges"][1:]}
    assert d.evidence_graph([thin], ev)[0] == d.FAIL
    assert d.evidence_graph([], ev)[0] == d.FAIL


def test_mtti_quotes_measured_numbers_or_nothing() -> None:
    rep = {"manual": {"cases": 6, "identified": 5, "seconds_when_identified": {"median": 420.0}},
           "ai_all": {"cases": 17, "identified": 13, "seconds_when_identified": {"median": 46.0}},
           "paired": {"ai_first": 5, "manual_first": 0},
           "ai_runs": [{"paid_models": ["claude-sonnet-5-5"]}]}  # fmt: skip
    step = d.mtti(rep, True)
    assert step[0] == d.PASS and "AI identified 13/17 (median 46.0 s)" in step[1]
    assert d.mtti(None, False)[0] == d.NA and d.mtti(None, True)[0] == d.FAIL
    assert d.mtti({**rep, "manual": {**rep["manual"], "cases": 5}}, True)[0] == d.FAIL
    assert d.mtti({**rep, "ai_runs": [{"paid_models": []}]}, True)[0] == d.FAIL


def test_mtti_with_the_manual_side_declared_invalid_quotes_the_ai_only() -> None:
    """Owner decision 2026-10-29: the manual baseline was not a real investigation; the demo
    says so and quotes no comparison (the paired 6-0 must never appear)."""
    rep = {"manual_valid": False, "manual_invalid_reason": "5 cases answered in 1-3 min, no metrics",
           "manual": {"cases": 6, "identified": 0, "seconds_when_identified": {"median": None}},
           "ai_all": {"cases": 17, "identified": 14, "seconds_when_identified": {"median": 35.7}},
           "paired": {"ai_first": 6, "manual_first": 0},
           "ai_runs": [{"paid_models": ["claude-sonnet-5-5"]}]}  # fmt: skip
    status, detail = d.mtti(rep, True)
    assert status == d.PASS and detail.startswith("AI ONLY - identified 14/17")
    assert "NO valid human baseline" in detail and "6" not in detail.split("baseline")[0][-10:]
    assert "first" not in detail  # no paired claim
    assert d.mtti({**rep, "ai_runs": [{"paid_models": []}]}, True)[0] == d.FAIL
