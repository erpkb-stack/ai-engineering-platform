"""Critic agent (architecture.md §10 #10). ADR-021.

Independence (.claude/rules/langgraph-agents.md): the critic gets the EVIDENCE (observations
rebuilt from the evidence agents' results) and the candidates as CODE wrote them - statement
and refs. Never the ranker's explanation or rank: it must not grade a story it was told.

  1 observations (code, rebuilt - must give the same refs as the hypothesis agent did)
  2 critique (LLM): verdict, contradicting refs, missing evidence per candidate; alternatives
  3 merge (code): a contradiction counts only if its ref exists, is not the candidate's own
    support, AND code says that observation can contradict that kind of cause
    (`candidates.can_contradict`); then the band is recomputed and the status is CHALLENGED.
    Other cited refs are kept as `critic_disputed` (shown, never in the band): the critic
    cannot move a band with an argument code cannot check (first real run, ADR-021 D).
    An alternative needs >= 1 valid supporting ref; its band is capped at MEDIUM. An
    alternative the critic marks `refines: hN` is stored as hN's `refinement` (a more specific
    mechanism, not a rival) only if hN is a candidate deploy and the alternative cites that
    deploy's own observation; otherwise it stays a rival.
  4 degrade: LLM down -> candidates unchanged, `critique.ran = false` (the run is PARTIAL)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from aeoi_agents.config import ReasoningBudget
from aeoi_agents.prompts import Prompt, load_prompt
from aeoi_agents.reasoning import llm as llm_call
from aeoi_agents.reasoning.candidates import can_contradict, evidence_of, rubric
from aeoi_agents.reasoning.hypothesis import candidate_block, observation_block
from aeoi_agents.reasoning.observations import build
from aeoi_agents.trace import Tracer
from aeoi_llm_client import LLMClient
from aeoi_models.api.agents import AgentRunResult
from aeoi_models.api.hypotheses import (
    Critique,
    HypothesisOut,
    Observation,
    observations_fingerprint,
)
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_models.findings import ConfidenceBand

AGENT_NAME = "critic"
AGENT_VERSION = "1.0.0"
PROMPT_VERSION = 2  # v2: "contradicting" defined; `refines` (first real run)
REF = {"type": "string", "pattern": r"^o[0-9]{1,3}$"}
MAX_ALTERNATIVES = 2
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": r"^h[0-9]{1,2}$"},
                    "verdict": {"type": "string", "enum": ["supported", "weakened", "refuted"]},
                    "contradicting": {"type": "array", "maxItems": 4, "items": REF},
                    "missing": {"type": "string", "maxLength": 200},
                },
                "required": ["id", "verdict", "contradicting", "missing"],
                "additionalProperties": False,
            },
        },
        "alternatives": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string", "maxLength": 300},
                    "supporting": {"type": "array", "maxItems": 4, "items": REF},
                    # the candidate id this is a more specific mechanism of, or "" for a rival
                    "refines": {"type": "string", "maxLength": 4},
                },
                "required": ["statement", "supporting", "refines"],
                "additionalProperties": False,
            },
        },
        "no_alternative_reason": {"type": "string", "maxLength": 300},
    },
    "required": ["reviews", "alternatives", "no_alternative_reason"],
    "additionalProperties": False,
}  # fmt: skip


@dataclass
class Deps:
    llm: LLMClient
    budget: ReasoningBudget


class CriticAgent:
    def __init__(self, deps: Deps, prompt: Prompt | None = None) -> None:
        self.deps = deps
        self.prompt = prompt or load_prompt(AGENT_NAME, PROMPT_VERSION)

    async def run(
        self, task: ReasoningTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        b = self.deps.budget
        deadline = time.monotonic() + b.task_deadline_s
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        tracer.trace.prompt_id = self.prompt.prompt_id
        tracer.trace.prompt_version = self.prompt.version
        tracer.trace.prompt_sha256 = self.prompt.sha256
        obs = build(task.results)
        by_ref = {o.ref: o for o in obs}
        # what the critic attacks: code fields only (explanation/rank are dropped HERE too)
        hs = [
            h.model_copy(update={"explanation": "", "explanation_refs": [], "ranked_by": "rubric"})
            for h in sorted(task.hypotheses, key=lambda h: h.key)
        ]
        if not hs:
            return AgentRunResult(
                status="SUCCEEDED", observations=obs, critique=Critique(ran=False,
                error="nothing to critique"), trace=tracer.finish(),
            )  # fmt: skip
        if task.observations_sha and task.observations_sha != observations_fingerprint(obs):
            return AgentRunResult(
                status="FAILED", trace=tracer.finish(),
                error="observation mismatch: the critic rebuilt different evidence than the ranker",
            )  # fmt: skip
        missing = {r for h in hs for r in [*h.supporting, *h.contradicting] if r not in by_ref}
        if missing:  # the two agents disagree on the evidence: refuse rather than guess
            return AgentRunResult(
                status="FAILED", trace=tracer.finish(),
                error=f"observation mismatch: refs {sorted(missing)[:5]} not in the evidence",
            )  # fmt: skip
        user = (
            "OBSERVATIONS (time order)\n" + observation_block(obs)
            + "\n\nCANDIDATES (written by code)\n" + candidate_block(hs)
            + "\n\nReview every candidate, then propose alternatives or say why there are none."
        )  # fmt: skip
        data, error = await llm_call.structured(
            self.deps.llm, tracer, self.prompt, user, SCHEMA,
            # the critic's route is NOT overridable per task: comparisons change the ranker only
            agent=AGENT_NAME, route=b.critic_route, max_tokens=b.critic_max_tokens,
            allow_fallback=b.critic_allow_fallback,
            timeout_s=max(1.0, min(b.llm_timeout_s, deadline - time.monotonic() - 5)),
            use_cache=task.llm_cache, investigation_id=task.investigation_id,
            schema_name="critique",
        )  # fmt: skip
        if data is None:
            return AgentRunResult(
                status="SUCCEEDED", degraded=f"not critiqued: {error}", observations=obs,
                hypotheses=hs, critique=Critique(ran=False, error=error), trace=tracer.finish(),
            )  # fmt: skip
        merged, critique = merge(hs, obs, data)
        critique.model = tracer.trace.model
        return AgentRunResult(
            status="SUCCEEDED", observations=obs, hypotheses=merged, critique=critique,
            trace=tracer.finish(),
        )  # fmt: skip


def merge(
    hs: list[HypothesisOut], obs: list[Observation], data: dict[str, Any]
) -> tuple[list[HypothesisOut], Critique]:
    by_ref = {o.ref: o for o in obs}
    by_key = {h.key: h for h in hs}
    c = Critique(ran=True)
    seen: set[str] = set()
    for rv in data.get("reviews", []):
        key = str(rv.get("id", ""))
        h = by_key.get(key)
        if h is None or key in seen:
            c.citations_dropped += 1
            continue
        seen.add(key)
        c.reviews += 1
        cited = [r for r in rv.get("contradicting", []) if isinstance(r, str)]
        fresh = [r for r in dict.fromkeys(cited)
                 if r in by_ref and r not in h.supporting and r not in h.contradicting]  # fmt: skip
        new = [r for r in fresh if can_contradict(h, by_ref[r], by_ref)]
        disputed = [r for r in fresh if r not in new]
        c.citations_dropped += len(cited) - len(fresh)
        c.contradictions_added += len(new)
        c.contradictions_ineligible += len(disputed)
        con = [*h.contradicting, *new]
        updated = h.model_copy(update={
            "contradicting": con,
            "contradicting_evidence": evidence_of(con, by_ref),
            "critic_verdict": rv.get("verdict", "weakened"),
            "critic_missing": " ".join(str(rv.get("missing", "")).split())[:300],
            "critic_disputed": disputed[:8],
            "status": "CHALLENGED" if new else h.status,
        })  # fmt: skip
        band, why = rubric(updated, by_ref)
        by_key[key] = updated.model_copy(update={"confidence": band, "rubric": why})
    # a candidate deploy's own deploy observation -> that hypothesis (for refinements)
    deploy_of = {h.supporting[0]: h.key for h in hs
                 if h.origin == "code" and h.cause_kind == "change" and h.supporting}  # fmt: skip
    alts: list[HypothesisOut] = []
    for alt in data.get("alternatives", []):
        c.alternatives_proposed += 1
        sup = list(dict.fromkeys(r for r in alt.get("supporting", []) if r in by_ref))
        text = " ".join(str(alt.get("statement", "")).split())[:300]
        if not sup or len(text) < 10:
            c.citations_dropped += len(alt.get("supporting", [])) - len(sup)
            continue
        # the critic DECLARES a refinement; code accepts it only for a candidate deploy whose
        # own deploy observation the alternative cites. Otherwise it stays a rival: guessing
        # "mechanism vs rival" from refs alone could hide a real rival (first real run)
        target_key = str(alt.get("refines") or "").strip()
        target = by_key.get(target_key)
        own = target.supporting[0] if target is not None and target.supporting else ""
        if target is not None and deploy_of.get(own) == target_key and own in sup:
            if not target.refinement:
                by_key[target.key] = target.model_copy(
                    update={"refinement": text, "refinement_refs": sup[:4]}
                )
            c.alternatives_merged += 1
            continue
        if len(alts) >= MAX_ALTERNATIVES:
            continue
        h = HypothesisOut(
            key=f"h{len(hs) + len(alts) + 1}", cause_kind="alternative", origin="critic",
            statement=text, rank=len(hs) + len(alts) + 1, confidence=ConfidenceBand.LOW,
            rubric="", supporting=sup, supporting_evidence=evidence_of(sup, by_ref),
            critic_verdict="not_reviewed",
        )  # fmt: skip
        band, why = rubric(h, by_ref)
        alts.append(h.model_copy(update={"confidence": band, "rubric": why}))
    c.alternatives_accepted = len(alts)
    c.no_alternative_reason = " ".join(str(data.get("no_alternative_reason", "")).split())[:400]
    return [by_key[h.key] for h in hs] + alts, c
