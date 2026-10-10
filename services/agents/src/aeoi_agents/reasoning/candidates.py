"""Candidate causes from observations, and the confidence rubric. Code only (ADR-021).

The owner's rule: code proposes, the LLM ranks. A candidate exists only when an observation
can be the CAUSE and it is not later than what it would explain:
- change:     a deploy that started no later than the first symptom (support: its own
              service only; other early deploys count against it)
- saturation: a resource signal; symptoms that started BEFORE it are listed as contradicting
              (they suggest the saturation is a consequence, not the cause)
- traffic:    a request-rate change
Ruled out (negative evidence): a deploy that started after the first symptom; a resource or
traffic metric that stayed within its baseline.
"""

from __future__ import annotations

from aeoi_agents.common import hhmm
from aeoi_agents.reasoning.observations import first_effect, minutes
from aeoi_models.api.hypotheses import HypothesisOut, Observation, RuledOut
from aeoi_models.findings import ConfidenceBand, EvidenceRef

RESOURCE_WORDS = {
    "db_pool_utilization": "DB connection pool saturation",
    "thread_pool_utilization": "thread pool saturation",
    "db_connections_active": "DB connection count growth",
    "cpu_util": "CPU saturation",
}
MAX_CANDIDATES = 8
MAX_HIGH_LEAD_MIN = 60


def evidence_of(refs: list[str], by_ref: dict[str, Observation]) -> list[EvidenceRef]:
    out: dict[str, EvidenceRef] = {}
    for r in refs:
        for e in by_ref[r].evidence:
            out.setdefault(e.evidence_id, e)
    return list(out.values())


def rubric(h: HypothesisOut, by_ref: dict[str, Observation]) -> tuple[ConfidenceBand, str]:
    """Evidence rubric, never an LLM number (.claude/rules/langgraph-agents.md).
    HIGH: >= 3 supporting observations from >= 2 independent sources, nothing contradicting.
    MEDIUM: >= 2 sources with at most 1 contradiction, or 1 source with >= 2 and none against.
    LOW: anything else. A critic's alternative is capped at MEDIUM (LLM-grouped evidence).
    A deploy more than MAX_HIGH_LEAD_MIN before the first symptom is capped at MEDIUM: "every
    symptom came after it" is true of ANY earlier deploy - timing alone is weak evidence.
    HIGH is a strength-of-evidence band, not proof; a human confirms (Phase 16)."""
    sources = {by_ref[r].agent for r in h.supporting}
    n_sup, n_con = len(h.supporting), len(h.contradicting)
    if len(sources) >= 2 and n_con == 0 and n_sup >= 3:
        band = ConfidenceBand.HIGH
    elif (len(sources) >= 2 and n_con <= 1) or (len(sources) == 1 and n_con == 0 and n_sup >= 2):
        band = ConfidenceBand.MEDIUM
    else:
        band = ConfidenceBand.LOW
    if h.origin == "critic" and band == ConfidenceBand.HIGH:
        band = ConfidenceBand.MEDIUM
    lead = ""
    if h.cause_kind == "change" and h.supporting:
        cause = by_ref[h.supporting[0]]
        effects = [by_ref[r] for r in h.supporting[1:] if by_ref[r].role == "effect"]
        if effects:
            m = minutes(cause.at, min(e.at for e in effects))
            lead = f"; lead time {m:g} min"
            if m > MAX_HIGH_LEAD_MIN and band == ConfidenceBand.HIGH:
                band = ConfidenceBand.MEDIUM
                lead += f" (> {MAX_HIGH_LEAD_MIN} min: capped)"
    why = (
        f"{n_sup} supporting from {len(sources)} source(s) ({', '.join(sorted(sources))}); "
        f"{n_con} contradicting{lead}"
    )
    return band, why[:300]


def can_contradict(h: HypothesisOut, o: Observation, by_ref: dict[str, Observation]) -> bool:
    """May observation `o` count AGAINST hypothesis `h`? CODE decides; the critic only points.

    Found on the first real run (Mac, Sonnet critic): the critic listed "cpu_util stayed
    within baseline" and "traffic stayed within baseline" as contradicting the deploy, and
    code counted any existing ref - so the deploy fell from HIGH to LOW on evidence that
    rules OUT its rivals. A steady metric or a downstream deploy cannot contradict a deploy.
    - change:     a sustained symptom on its own service that started BEFORE the deploy, or
                  another deploy that started no later than this hypothesis' first symptom
    - saturation / traffic: a symptom on the same service that started before it, or the same
                  metric reported steady
    - a critic alternative is never reviewed, so nothing contradicts it here"""
    if h.origin != "code" or not h.supporting or h.supporting[0] not in by_ref:
        return False
    cause = by_ref[h.supporting[0]]
    if o.ref == cause.ref:
        return False
    if h.cause_kind == "change":
        if (o.role == "effect" and o.sustained and o.service_key == cause.service_key
                and o.at < cause.at):  # fmt: skip
            return True
        firsts = [by_ref[r].at for r in h.supporting if r in by_ref and by_ref[r].role == "effect"]
        return o.role == "change" and bool(firsts) and o.at <= min(firsts)
    if h.cause_kind in ("saturation", "traffic"):
        if o.service_key != cause.service_key:
            return False
        if o.role == "effect" and o.at < cause.at:
            return True
        return o.role == "steady" and o.subject == cause.subject
    return False


MAX_RULED_OUT = 12  # AgentRunResult.ruled_out cap (review finding: 13 crashed the agent)


def propose(obs: list[Observation]) -> tuple[list[HypothesisOut], list[RuledOut]]:
    """Wording says "may have": nothing is validated yet (review finding)."""
    by_ref = {o.ref: o for o in obs}
    first = first_effect(obs)
    if first is None:
        return [], []
    effects = [o for o in obs if o.role == "effect"]
    drafts: list[dict[str, object]] = []
    ruled: list[RuledOut] = []
    early = [c for c in obs if c.role == "change" and c.at <= first.at]
    for c in (o for o in obs if o.role == "change"):
        if c.at <= first.at:
            # support: symptoms and saturation on the deploy's OWN service, after it started.
            # Another deploy that also precedes the first symptom is a competing explanation:
            # it counts AGAINST (review finding: every early deploy was HIGH by construction)
            sup = [c.ref] + [
                o.ref for o in obs
                if o.role in ("effect", "resource") and o.at >= c.at
                and o.service_key == c.service_key
            ]  # fmt: skip
            con = [o.ref for o in early if o.ref != c.ref]
            drafts.append({
                "cause_kind": "change", "supporting": sup, "contradicting": con,
                "statement": f"Deploy {c.subject} on {c.service_key} may have caused the "
                f"regression: it started {minutes(c.at, first.at):g} min before the first "
                f"symptom ({first.subject})"
                + (f"; {len(con)} other deploy(s) also started before it." if con else "."),
            })  # fmt: skip
        elif first.sustained:
            ruled.append(RuledOut(
                cause_kind="change", contradicting=[first.ref],
                contradicting_evidence=evidence_of([first.ref], by_ref),
                statement=f"Deploy {c.subject} did not start the regression: it started "
                f"{minutes(first.at, c.at):g} min AFTER the first sustained symptom "
                f"({first.subject}).",
            ))  # fmt: skip
        # a deploy after a log-only or transient "first symptom" is neither proposed nor ruled
        # out: that anchor can be background noise (review finding)
    for r in (o for o in obs if o.role == "resource"):
        sup = [r.ref] + [e.ref for e in effects if e.at >= r.at]
        con = [e.ref for e in effects if e.at < r.at]
        what = RESOURCE_WORDS.get(r.subject, f"{r.subject} saturation")
        if len(sup) == 1 and first.sustained and first.at < r.at:
            ruled.append(RuledOut(
                cause_kind="saturation", contradicting=con[:4],
                contradicting_evidence=evidence_of(con[:4], by_ref),
                statement=f"{what} on {r.service_key} is a consequence, not the trigger: "
                f"{r.subject} rose from {hhmm(r.at)}, after every symptom.",
            ))  # fmt: skip
            continue
        drafts.append({
            "cause_kind": "saturation", "supporting": sup, "contradicting": con,
            "statement": f"{what} on {r.service_key} may have driven the symptoms: {r.subject} "
            f"rose from {hhmm(r.at)}"
            + (f"; {len(con)} symptom(s) started earlier." if con else "."),
        })  # fmt: skip
    for t in (o for o in obs if o.role == "traffic"):
        sup = [t.ref] + [e.ref for e in effects if e.at >= t.at]
        con = [e.ref for e in effects if e.at < t.at]
        drafts.append({
            "cause_kind": "traffic", "supporting": sup, "contradicting": con,
            "statement": f"A traffic change ({t.subject}) on {t.service_key} may have driven "
            "the symptoms.",
        })  # fmt: skip
    for s in (o for o in obs if o.role == "steady"):
        if s.subject in RESOURCE_WORDS or s.subject in ("requests_per_sec",):
            kind = "traffic" if s.subject == "requests_per_sec" else "saturation"
            what = "A traffic surge" if kind == "traffic" else RESOURCE_WORDS[s.subject]
            ruled.append(RuledOut(
                cause_kind=kind, contradicting=[s.ref],  # type: ignore[arg-type]
                contradicting_evidence=evidence_of([s.ref], by_ref),
                statement=f"{what} on {s.service_key} is ruled out: {s.subject} stayed within "
                "its baseline.",
            ))  # fmt: skip
    ruled = ruled[:MAX_RULED_OUT]
    out: list[HypothesisOut] = []
    for i, d in enumerate(drafts[:MAX_CANDIDATES]):
        sup = list(d["supporting"])  # type: ignore[call-overload]
        con = list(d["contradicting"])  # type: ignore[call-overload]
        h = HypothesisOut(
            key=f"h{i + 1}", cause_kind=d["cause_kind"], origin="code",  # type: ignore[arg-type]
            statement=str(d["statement"])[:600], rank=i + 1, confidence=ConfidenceBand.LOW,
            rubric="", supporting=sup, contradicting=con,
            supporting_evidence=evidence_of(sup, by_ref),
            contradicting_evidence=evidence_of(con, by_ref),
        )  # fmt: skip
        band, why = rubric(h, by_ref)
        out.append(h.model_copy(update={"confidence": band, "rubric": why}))
    return out, ruled


BAND_ORDER = {ConfidenceBand.HIGH: 0, ConfidenceBand.MEDIUM: 1, ConfidenceBand.LOW: 2}


def rubric_order(hs: list[HypothesisOut]) -> list[HypothesisOut]:
    """Fallback ranking when the model is unavailable: band, then support, then key."""
    ordered = sorted(
        hs,
        key=lambda h: (BAND_ORDER[h.confidence], -len(h.supporting), len(h.contradicting), h.key),
    )
    return [h.model_copy(update={"rank": i + 1}) for i, h in enumerate(ordered)]
