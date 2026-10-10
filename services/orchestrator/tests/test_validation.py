"""Deterministic hypothesis validation + the Phase 11 outcome (ADR-021). Pure, no IO."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aeoi_models.api.agents import AgentRunResult, AgentTrace
from aeoi_models.api.hypotheses import Critique, HypothesisOut, Observation
from aeoi_models.findings import ConfidenceBand, EvidenceKind, EvidenceRef
from aeoi_orchestrator.graph import reasoning_outcome
from aeoi_orchestrator.validation import merge, to_batch, validate

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
HEX = uuid4().hex


def e(kind: str, n: int) -> EvidenceRef:
    return EvidenceRef(evidence_id=f"{kind}-{HEX}-{n}", kind=EvidenceKind(kind), source_system="s")


def obs(ref: str, role: str, minute: int, ev: EvidenceRef) -> Observation:
    return Observation(ref=ref, agent="metrics", role=role, service_key="svc",  # type: ignore[arg-type]
                       subject="m", at=T0 + timedelta(minutes=minute), statement="s", evidence=[ev],
                       sustained=role == "effect")  # fmt: skip


OBS = [obs("o1", "change", 0, e("DEPLOY", 1)), obs("o2", "effect", 5, e("METRIC", 2)),
       obs("o3", "resource", 6, e("METRIC", 3))]  # fmt: skip
STORED = {"DEPLOY-" + HEX + "-1", "METRIC-" + HEX + "-2", "METRIC-" + HEX + "-3"}


def h(key: str, sup: list[str], kind: str = "change", origin: str = "code", rank: int = 1,
      extra_ev: list[EvidenceRef] | None = None) -> HypothesisOut:  # fmt: skip
    by = {o.ref: o for o in OBS}
    return HypothesisOut(
        key=key, cause_kind=kind, origin=origin, statement="a cause", rank=rank,  # type: ignore[arg-type]
        confidence=ConfidenceBand.HIGH, rubric="r", supporting=sup,
        supporting_evidence=[by[r].evidence[0] for r in sup] + (extra_ev or []),
        explanation="ranker prose", explanation_refs=["o1"], critic_verdict="supported",
    )  # fmt: skip


def critic(
    hs: list[HypothesisOut],
    *,
    ran: bool = True,
    reason: str = "",
    alts: int = 0,
    merged: int = 0,
) -> AgentRunResult:
    return AgentRunResult(status="SUCCEEDED", hypotheses=hs,
                          critique=Critique(ran=ran, alternatives_accepted=alts,
                                            alternatives_merged=merged,
                                            no_alternative_reason=reason),
                          trace=AgentTrace(agent="critic", agent_version="1"))  # fmt: skip


def test_merge_keeps_rank_and_prose_takes_the_critics_verdict() -> None:
    ranked = [h("h1", ["o1", "o2"], rank=2), h("h2", ["o3"], kind="saturation", rank=1)]
    reviewed = ranked[0].model_copy(update={"status": "CHALLENGED", "contradicting": ["o3"],
                                            "critic_verdict": "weakened", "explanation": ""})  # fmt: skip
    alt = h("h3", ["o2"], kind="alternative", origin="critic", rank=9)
    out = merge(ranked, critic([reviewed, ranked[1], alt]))
    assert [x.key for x in out] == ["h1", "h2", "h3"]
    assert out[0].explanation == "ranker prose" and out[0].status == "CHALLENGED"
    assert out[0].rank == 2 and out[2].rank == 3


def test_unstored_citation_and_wrong_timing_are_dropped() -> None:
    ok = h("h1", ["o1", "o2"])
    ghost = h("h2", ["o1"], extra_ev=[e("LOG", 99)], rank=2)  # cites evidence never stored
    late = h("h3", ["o3"], kind="saturation", rank=3)  # saturation after every symptom
    kept, report = validate(
        [ok, ghost, late], OBS, STORED, critic([], reason="no other cause fits")
    )
    assert [k.key for k in kept] == ["h1"] and kept[0].status == "VALIDATED"
    assert report.dropped == ["h2", "h3"] and not report.passed
    failed = {c.name for c in report.checks if not c.passed}
    assert failed == {"citations_stored", "timing"}


def test_critic_must_answer_with_an_alternative_or_a_reason() -> None:
    _, r1 = validate([h("h1", ["o1", "o2"])], OBS, STORED, critic([], reason="x"))
    assert not r1.passed and [c.name for c in r1.checks if not c.passed] == ["critic_answered"]
    _, r2 = validate([h("h1", ["o1", "o2"])], OBS, STORED, critic([], alts=1))
    assert r2.passed
    _, r3 = validate([h("h1", ["o1", "o2"])], OBS, STORED, None)
    assert "critic did not run" in next(c.detail for c in r3.checks if c.name == "critic_answered")


def test_batch_marks_ruled_out_as_rejected() -> None:
    from aeoi_models.api.hypotheses import RuledOut

    ro = RuledOut(cause_kind="traffic", statement="A traffic surge is ruled out", contradicting=["o2"],
                  contradicting_evidence=[e("METRIC", 2)])  # fmt: skip
    b = to_batch(uuid4(), [h("h1", ["o1", "o2"])], [ro])
    assert b is not None
    assert [(i.key, i.status) for i in b.items] == [("h1", "PROPOSED"), ("r1", "REJECTED")]
    assert b.items[0].detail["explanation"] == "ranker prose"


def test_reasoning_outcome() -> None:
    ok = {"hypothesis": {"status": "SUCCEEDED"}, "critic": {"status": "SUCCEEDED"},
          "validation": {"kept": 2, "failed_checks": []}}  # fmt: skip
    assert reasoning_outcome("COMPLETE", None, ok) == ("COMPLETE", None)
    assert reasoning_outcome("PARTIAL", "missing sources - knowledge", ok)[0] == "PARTIAL"
    none = {**ok, "validation": {"kept": 0, "failed_checks": []}}
    assert reasoning_outcome("COMPLETE", None, none) == (
        "INCONCLUSIVE",
        "no hypothesis is supported by the evidence",
    )
    crit = {**ok, "critic": {"status": "FAILED", "error": "agent returned HTTP 500"},
            "validation": {"kept": 1, "failed_checks": ["critic_answered"]}}  # fmt: skip
    status, error = reasoning_outcome("COMPLETE", None, crit)
    assert status == "PARTIAL" and error and "critic: agent returned HTTP 500" in error
    assert reasoning_outcome("FAILED", "x", none) == ("FAILED", "x")  # worst wins
    assert reasoning_outcome("COMPLETE", None, {"skipped": True}) == ("COMPLETE", None)


def test_critic_on_the_rankers_model_is_flagged() -> None:
    """Owner decision: Haiku ranks, Sonnet critiques. Same model = not independent (PARTIAL)."""
    hs = [h("h1", ["o1", "o2"])]
    crit = critic([], reason="no other signal moved before the deploy")
    crit.critique.model = "claude-haiku-4-5-20251001"  # type: ignore[union-attr]
    _, rep = validate(hs, OBS, STORED, crit, ranker_model="claude-haiku-4-5-20251001")
    assert [c.name for c in rep.checks if not c.passed] == ["critic_independent"]
    _, rep = validate(hs, OBS, STORED, crit, ranker_model="claude-sonnet-5-5")
    assert all(c.passed for c in rep.checks)
    status, err = reasoning_outcome(
        "COMPLETE", None,
        {"hypothesis": {"status": "SUCCEEDED"},
         "validation": {"kept": 1, "failed_checks": ["critic_independent"]}},
    )  # fmt: skip
    assert status == "PARTIAL" and err and "not independent" in err


def test_validated_means_reviewed_and_not_refuted() -> None:
    """Review finding: alternatives and refuted/unreviewed candidates were stored VALIDATED."""
    ok = h("h1", ["o1", "o2"])
    refuted = h("h2", ["o1", "o2"], rank=2).model_copy(update={"critic_verdict": "refuted"})
    unreviewed = h("h3", ["o1", "o2"], rank=3).model_copy(update={"critic_verdict": "not_reviewed"})
    alt = h("h4", ["o2"], kind="alternative", origin="critic", rank=4)
    kept, _ = validate([ok, refuted, unreviewed, alt], OBS, STORED, critic([], reason="x" * 12))
    assert [(k.key, k.status) for k in kept] == [
        ("h1", "VALIDATED"),
        ("h2", "PROPOSED"),
        ("h3", "PROPOSED"),
        ("h4", "PROPOSED"),
    ]


def test_validated_needs_a_supported_verdict_weakened_stays_proposed() -> None:
    """First real run: a 'weakened' verdict with no checkable contradiction is an opinion.
    It must neither clear the hypothesis (VALIDATED) nor lower it (CHALLENGED)."""
    weak = h("h1", ["o1", "o2"]).model_copy(update={"critic_verdict": "weakened",
                                                   "critic_disputed": ["o3"]})  # fmt: skip
    kept, _ = validate([weak], OBS, STORED, critic([], reason="x" * 12))
    assert [(k.key, k.status) for k in kept] == [("h1", "PROPOSED")]


def test_a_refinement_counts_as_the_critics_answer_and_is_stored_by_name() -> None:
    ref = h("h1", ["o1", "o2"]).model_copy(update={
        "refinement": "the flag flip in the deploy is the trigger", "refinement_refs": ["o1", "o3"],
        "critic_disputed": ["o3"]})  # fmt: skip
    kept, rep = validate([ref], OBS, STORED, critic([], merged=1))
    assert next(c for c in rep.checks if c.name == "critic_answered").passed
    batch = to_batch(uuid4(), kept, [], OBS)
    assert batch is not None
    d = batch.items[0].detail
    assert d["refinement_refs"] == ["m", "m"] and d["critic_disputed"] == ["m"]  # subjects
    assert batch.items[0].contradicts == []  # a disputed ref is never a CONTRADICTS edge


def test_a_deploy_after_the_first_sustained_symptom_is_dropped() -> None:
    late_obs = [*OBS, obs("o4", "change", 9, e("DEPLOY", 4))]
    sustained = late_obs
    late = h("h2", ["o1"]).model_copy(update={"supporting": ["o4"],
              "supporting_evidence": [e("DEPLOY", 4)], "key": "h2", "rank": 2})  # fmt: skip
    _, rep = validate([h("h1", ["o1", "o2"]), late], sustained, {*STORED, f"DEPLOY-{HEX}-4"},
                      critic([], reason="x" * 12))  # fmt: skip
    assert rep.dropped == ["h2"]
    assert "after the first symptom" in next(c.detail for c in rep.checks if c.name == "timing")


def test_a_log_only_anchor_drops_nothing_on_timing() -> None:
    """Candidates and validation share one rule: a log cluster's first_seen may be background
    noise, so it cannot time a cause out (else code would drop its own candidates)."""
    logs_only = [o.model_copy(update={"sustained": False}) for o in OBS]
    late = h("h3", ["o3"], kind="saturation", rank=2)
    kept, rep = validate([h("h1", ["o1", "o2"]), late], logs_only, STORED,
                         critic([], reason="x" * 12))  # fmt: skip
    assert rep.dropped == [] and [k.key for k in kept] == ["h1", "h3"]
