"""Phase 16 (ADR-026): the code rule that decides whether an investigation proposes a rollback.
Every branch that says "no" must say why; the one that says "yes" proposes exactly the deploy
the top validated hypothesis names, with a code-written snapshot for the approver."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from aeoi_models.api.agents import AgentRunResult, AgentTrace, ConfigChangeOut, DeployChange
from aeoi_models.api.approvals import args_sha256
from aeoi_models.api.code import CodeFinding
from aeoi_models.api.hypotheses import HypothesisOut, Observation
from aeoi_models.findings import ConfidenceBand, EvidenceRef
from aeoi_orchestrator.proposal import RULE, propose

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
INV = uuid4()
EV = EvidenceRef(evidence_id="DEPLOY-abc-0", kind="DEPLOY", source_system="devdata")


def _obs() -> list[Observation]:
    return [
        Observation(ref="o1", agent="deployment", role="change", service_key="checkout",
                    subject="DEPLOY-1042", at=T0, statement="deploy", evidence=[EV]),
        Observation(ref="o2", agent="metrics", role="effect", service_key="checkout",
                    subject="p95", at=T0 + timedelta(minutes=4), statement="p95 up",
                    evidence=[EV], sustained=True),
    ]  # fmt: skip


def _hyp(**kw: Any) -> HypothesisOut:
    base: dict[str, Any] = dict(
        key="h1", cause_kind="change", origin="code", rank=1, confidence=ConfidenceBand.HIGH,
        rubric="2 supporting", status="VALIDATED", supporting=["o1", "o2"],
        supporting_evidence=[EV], statement="Deploy DEPLOY-1042 on checkout may have caused it.",
    )  # fmt: skip
    return HypothesisOut(**{**base, **kw})


def _deploy(**kw: Any) -> AgentRunResult:
    change = DeployChange(**{**dict(
        deploy_key="DEPLOY-1042", service_key="checkout", version="2.4.0", status="SUCCEEDED",
        started_at=T0, commit_sha="b" * 40, previous_commit_sha="a" * 40, deployed_by="ci",
        evidence_ids=["DEPLOY-abc-0"],
        config_changes=[ConfigChangeOut(key="checkout.feature_new_pricing", before=False,
                                        after=True),
                        ConfigChangeOut(key="checkout.pool", before=10, after=10)],
    ), **kw})  # fmt: skip
    return AgentRunResult(status="SUCCEEDED", changes=[change],
                          trace=AgentTrace(agent="deployment", agent_version="1"))  # fmt: skip


def _code() -> AgentRunResult:
    f = CodeFinding(rule="query_in_loop", deploy_key="DEPLOY-1042", deploy_started_at=T0,
                    service_key="checkout", commit_sha="b" * 40, committed_at=T0,
                    path="src/pricing.py", line=42, symbol="price_all",
                    statement="query in loop", evidence_id="CODE-x-0")  # fmt: skip
    return AgentRunResult(status="SUCCEEDED", code_findings=[f],
                          trace=AgentTrace(agent="code_intelligence", agent_version="1"))  # fmt: skip


def test_top_validated_deploy_is_proposed_with_a_code_written_snapshot() -> None:
    req, why = propose(INV, [_hyp()], _obs(), _deploy(), _code())
    assert req is not None, why
    assert req.action == "rollback_deployment"
    assert req.args.deploy_key == "DEPLOY-1042"
    assert req.investigation_id == INV
    assert req.evidence_keys == ["DEPLOY-abc-0"]
    rec = req.recommendation
    assert rec["rule"] == RULE and rec["confidence"] == "HIGH"
    assert rec["config_restored"] == ["checkout.feature_new_pricing"]  # unchanged key left out
    way = " ".join(rec["way_back"])
    assert "a" * 12 in way and "newer deploy makes it refuse" in way
    assert "flag back alone" in way  # a changed flag never passes as "the way back"
    assert "src/pricing.py:42 price_all" in way  # code quoted ONLY under way_back (redacted)
    assert "src/pricing.py" not in str({k: v for k, v in rec.items() if k != "way_back"})


def test_same_inputs_give_the_same_request_hash() -> None:
    """The proposal is idempotent at incident-service per (investigation, args hash): a
    resumed graph must produce byte-identical arguments."""
    a, _ = propose(INV, [_hyp()], _obs(), _deploy(), _code())
    b, _ = propose(INV, [_hyp()], _obs(), _deploy(), _code())
    assert a is not None and b is not None
    ha = args_sha256(a.action, a.args.model_dump(mode="json"))
    assert ha == args_sha256(b.action, b.args.model_dump(mode="json"))


def test_every_no_says_why() -> None:
    cases = [
        (dict(kept=[]), "no hypothesis survived"),
        (dict(kept=[_hyp(cause_kind="saturation")]), "not a deploy"),
        (dict(kept=[_hyp(status="CHALLENGED")]), "not VALIDATED"),
        (dict(kept=[_hyp(supporting=["o2", "o1"])]), "does not name a deploy"),
        (dict(deployment=None), "not in the deployment agent's result"),
        (dict(deployment=_deploy(status="FAILED")), "nothing running"),
        (dict(deployment=_deploy(previous_commit_sha=None)), "no earlier successful deploy"),
    ]
    for kw, expected in cases:
        args: dict[str, Any] = dict(kept=[_hyp()], deployment=_deploy())
        args.update(kw)
        req, why = propose(INV, args["kept"], _obs(), args["deployment"], None)
        assert req is None, kw
        assert expected in why, (kw, why)


def test_only_the_top_hypothesis_counts() -> None:
    """A validated deploy ranked SECOND is not proposed: the evidence favours another cause."""
    top = _hyp(key="h1", cause_kind="saturation", supporting=["o2"])
    second = _hyp(key="h2", rank=2)
    req, why = propose(INV, [top, second], _obs(), _deploy(), None)
    assert req is None and "saturation" in why
