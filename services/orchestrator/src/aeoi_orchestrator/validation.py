"""Deterministic validation of hypotheses before they are stored (ADR-021). Pure functions.

No LLM judge in this phase (owner decision): every check here is code, so a failure has an
exact reason and a test. A hypothesis that fails a HARD check is dropped, never "fixed":
- citations_stored: every cited evidence id was stored on the incident by this investigation
- support_present:  at least one supporting evidence id
- timing:           a change/saturation cause is not later than the FIRST symptom (the earliest
                    sustained metric shift, else the earliest effect) - checked against the
                    timeline, not against the cause's own support list (review finding: that
                    list is built after the cause, so the old check could never fail)
SOFT checks (recorded; they make the run PARTIAL, they drop nothing):
- critic_answered:  the critic ran and gave an alternative (a rival, or a refinement of a
                    candidate deploy), or a reason why there is none
- ranks_unique:     ranks are 1..n without gaps
- critic_independent: the critic's model differs from the ranker's (owner decision: Haiku
                    ranks, Sonnet critiques; a config change that makes them equal is caught)
"""

from __future__ import annotations

from aeoi_models.api.agents import AgentRunResult
from aeoi_models.api.hypotheses import (
    HypothesisBatch,
    HypothesisIn,
    HypothesisOut,
    Observation,
    RuledOut,
    ValidationCheck,
    ValidationReport,
)
from aeoi_models.findings import ConfidenceBand

CRITIC_FIELDS = (
    "contradicting",
    "contradicting_evidence",
    "confidence",
    "rubric",
    "status",
    "critic_verdict",
    "critic_missing",
    "critic_disputed",
    "refinement",
    "refinement_refs",
)
MIN_REASON = 10


def merge(ranked: list[HypothesisOut], critic: AgentRunResult | None) -> list[HypothesisOut]:
    """The ranker's order + explanation, with the critic's verdict/contradictions/band laid
    over by key, then the critic's alternatives after the ranked ones."""
    if critic is None or critic.critique is None or not critic.critique.ran:
        return ranked
    reviewed = {h.key: h for h in critic.hypotheses if h.origin == "code"}
    out = [
        h.model_copy(update={f: getattr(reviewed[h.key], f) for f in CRITIC_FIELDS})
        if h.key in reviewed
        else h
        for h in ranked
    ]
    alts = [h for h in critic.hypotheses if h.origin == "critic"]
    return out + [a.model_copy(update={"rank": len(out) + i + 1}) for i, a in enumerate(alts)]


def validate(
    hs: list[HypothesisOut],
    observations: list[Observation],
    stored_keys: set[str],
    critic: AgentRunResult | None,
    ranker_model: str | None = None,
) -> tuple[list[HypothesisOut], ValidationReport]:
    by_ref = {o.ref: o for o in observations}
    effects = [o for o in observations if o.role == "effect"]
    anchors = [o for o in effects if o.sustained] or effects
    first_at = min((o.at for o in anchors), default=None)
    # same rule as the candidates: only a SUSTAINED metric shift can time a cause out; a log
    # cluster's first_seen may be background noise (they must agree, or code drops its own work)
    sustained_anchor = any(o.sustained for o in effects)
    checks: list[ValidationCheck] = []
    dropped: dict[str, str] = {}
    for h in hs:
        cited = {e.evidence_id for e in [*h.supporting_evidence, *h.contradicting_evidence]}
        unknown = sorted(cited - stored_keys)
        if unknown:
            dropped[h.key] = f"citations_stored: {', '.join(unknown[:3])} not stored"
            continue
        if not h.supporting_evidence:
            dropped[h.key] = "support_present: no supporting evidence"
            continue
        if h.origin == "code" and h.cause_kind in ("change", "saturation"):
            cause = by_ref.get(h.supporting[0])
            sup_effects = [
                r for r in h.supporting[1:] if r in by_ref and by_ref[r].role == "effect"
            ]
            if cause is None:
                dropped[h.key] = "timing: the cause is not in the evidence"
            elif h.cause_kind == "change" and sustained_anchor and first_at and cause.at > first_at:
                dropped[h.key] = "timing: the deploy started after the first symptom"
            elif h.cause_kind == "saturation" and not sup_effects and sustained_anchor:
                dropped[h.key] = "timing: the saturation started after every symptom"
    checks.append(ValidationCheck(
        name="citations_stored", passed=not any(v.startswith("citations") for v in dropped.values()),
        detail="; ".join(f"{k}: {v}" for k, v in dropped.items() if v.startswith("citations")),
    ))  # fmt: skip
    for name in ("support_present", "timing"):
        bad = {k: v for k, v in dropped.items() if v.startswith(name)}
        checks.append(ValidationCheck(name=name, passed=not bad,
                                      detail="; ".join(f"{k}: {v}" for k, v in bad.items())))  # fmt: skip
    kept = [h for h in hs if h.key not in dropped]
    kept = [
        h.model_copy(update={"rank": i + 1, "status": _status(h)})
        for i, h in enumerate(sorted(kept, key=lambda h: h.rank))
    ]
    c = critic.critique if critic is not None else None
    gave = (c.alternatives_accepted + c.alternatives_merged) if c else 0
    answered = bool(c and c.ran and (gave > 0 or len(c.no_alternative_reason) >= MIN_REASON))
    checks.append(ValidationCheck(
        name="critic_answered", passed=answered,
        detail="" if answered else (
            "critic did not run" if not (c and c.ran)
            else "critic gave no valid alternative and no reason"
        ),
    ))  # fmt: skip
    critic_model = c.model if (c and c.ran) else None
    same = bool(critic_model and ranker_model and critic_model == ranker_model)
    checks.append(ValidationCheck(
        name="critic_independent", passed=not same,
        detail=f"critic and ranker are both {critic_model}" if same else "",
    ))  # fmt: skip
    ranks = [h.rank for h in kept]
    checks.append(
        ValidationCheck(name="ranks_unique", passed=ranks == list(range(1, len(kept) + 1)))
    )
    hard = all(
        ch.passed for ch in checks if ch.name in ("citations_stored", "support_present", "timing")
    )
    report = ValidationReport(passed=hard and answered, checks=checks, dropped=sorted(dropped))
    return kept, report


def _status(h: HypothesisOut) -> str:
    """VALIDATED means: passed every hard check AND the critic called it supported.
    CHALLENGED: the critic cited evidence code accepts as contradicting it. Anything else the
    critic said (weakened / refuted without checkable evidence) leaves it PROPOSED for a
    human - it neither clears nor lowers it (first real run, ADR-021 D)."""
    if h.status == "CHALLENGED":
        return "CHALLENGED"
    if h.origin == "code" and h.critic_verdict == "supported":
        return "VALIDATED"
    return "PROPOSED"


def _named(refs: list[str], by_ref: dict[str, Observation]) -> list[str]:
    """o7 -> 'cpu_util': a stored ref must mean something outside this run's trace."""
    return [by_ref[r].subject if r in by_ref else r for r in refs]


def to_batch(
    investigation_id: object,
    hs: list[HypothesisOut],
    ruled: list[RuledOut],
    observations: list[Observation] | None = None,
) -> HypothesisBatch | None:
    by_ref = {o.ref: o for o in observations or []}
    items = [
        HypothesisIn(
            key=h.key, statement=h.statement, confidence=h.confidence, status=h.status,
            rank=h.rank, produced_by="critic" if h.origin == "critic" else "hypothesis",
            supports=[e.evidence_id for e in h.supporting_evidence],
            contradicts=[e.evidence_id for e in h.contradicting_evidence],
            detail={
                "cause_kind": h.cause_kind, "origin": h.origin, "rubric": h.rubric,
                "ranked_by": h.ranked_by, "explanation": h.explanation or None,
                "explanation_refs": h.explanation_refs, "critic_verdict": h.critic_verdict,
                "critic_missing": h.critic_missing or None,
                "critic_disputed": _named(h.critic_disputed, by_ref) or None,
                "refinement": h.refinement or None,
                "refinement_refs": _named(h.refinement_refs, by_ref) or None,
            },
        )
        for h in hs
    ] + [
        HypothesisIn(
            key=f"r{i + 1}", statement=r.statement, confidence=ConfidenceBand.LOW,
            status="REJECTED", rank=len(hs) + i + 1, produced_by="hypothesis",
            contradicts=[e.evidence_id for e in r.contradicting_evidence],
            detail={"cause_kind": r.cause_kind, "origin": "code", "ruled_out": "yes"},
        )
        for i, r in enumerate(ruled[:10])
    ]  # fmt: skip
    if not items:
        return None
    return HypothesisBatch.model_validate({"investigation_id": investigation_id, "items": items})
