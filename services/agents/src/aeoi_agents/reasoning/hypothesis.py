"""Hypothesis agent (architecture.md §9 'hypothesize'). ADR-021.

  1 observations (code): the evidence agents' typed results as a timeline with refs o1..oN
  2 candidates  (code): causes that could explain the symptoms + ruled-out causes + rubric band
  3 rank        (LLM):  order the candidates and explain the mechanism, citing refs; validated:
                        unknown ids dropped, refs that do not exist dropped, an explanation with
                        no valid ref is dropped (uncited prose is not shown)
  4 degrade:            LLM down -> rubric order, `ranked_by=rubric`, result marked degraded
The LLM cannot add a cause, change a band, or cite evidence that was not observed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from aeoi_agents.config import ReasoningBudget
from aeoi_agents.prompts import Prompt, load_prompt
from aeoi_agents.reasoning import llm as llm_call
from aeoi_agents.reasoning.candidates import propose, rubric_order
from aeoi_agents.reasoning.observations import build
from aeoi_agents.trace import Tracer
from aeoi_llm_client import LLMClient
from aeoi_models.api.agents import AgentRunResult
from aeoi_models.api.hypotheses import HypothesisOut, Observation
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_security.untrusted import wrap_untrusted

AGENT_NAME = "hypothesis"
AGENT_VERSION = "1.0.0"
PROMPT_VERSION = 1
REF = r"^o[0-9]{1,3}$"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ranking": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": r"^h[0-9]{1,2}$"},
                    "explanation": {"type": "string", "maxLength": 400},
                    "refs": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "string", "pattern": REF},
                    },
                },
                "required": ["id", "explanation", "refs"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranking"],
    "additionalProperties": False,
}


@dataclass
class Deps:
    llm: LLMClient
    budget: ReasoningBudget


def observation_block(obs: list[Observation]) -> str:
    body = "\n".join(f"{o.ref} [{o.role}] {o.statement}" for o in obs)
    return wrap_untrusted(body, source="evidence-agents", item_id="observations", max_chars=12_000)


def candidate_block(hs: list[HypothesisOut]) -> str:
    """Code statements and refs ONLY - never an LLM explanation (critic independence)."""
    body = "\n".join(
        f"{h.key} {h.statement} support={','.join(h.supporting)}"
        + (f" against={','.join(h.contradicting)}" if h.contradicting else "")
        for h in hs
    )
    return wrap_untrusted(body, source="candidates", item_id="candidates", max_chars=8_000)


class HypothesisAgent:
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
        cands, ruled = propose(obs)
        notes: list[str] = []
        if not cands:
            notes.append("no candidate cause: no symptom observation (error/latency/log) found.")
            return AgentRunResult(
                status="SUCCEEDED", notes=notes, observations=obs, ruled_out=ruled,
                trace=tracer.finish(),
            )  # fmt: skip
        user = (
            "OBSERVATIONS (time order)\n" + observation_block(obs)
            + "\n\nCANDIDATES\n" + candidate_block(cands)
            + "\n\nRank every candidate and explain each one."
        )  # fmt: skip
        data, error = await llm_call.structured(
            self.deps.llm, tracer, self.prompt, user, SCHEMA,
            agent=AGENT_NAME, route=task.llm_route or b.rank_route, max_tokens=b.rank_max_tokens,
            timeout_s=max(1.0, min(b.llm_timeout_s, deadline - time.monotonic() - 5)),
            use_cache=task.llm_cache, investigation_id=task.investigation_id,
            schema_name="hypothesis_ranking",
        )  # fmt: skip
        ranked, degraded = self._apply(cands, obs, data, notes)
        if error:
            degraded = f"ranked by rubric: {error}"
        return AgentRunResult(
            status="SUCCEEDED",
            degraded=degraded,
            notes=notes[:30],
            observations=obs,
            hypotheses=ranked,
            ruled_out=ruled,
            trace=tracer.finish(),
        )

    @staticmethod
    def _apply(
        cands: list[HypothesisOut],
        obs: list[Observation],
        data: dict[str, Any] | None,
        notes: list[str],
    ) -> tuple[list[HypothesisOut], str | None]:
        if data is None:
            return rubric_order(cands), None
        by_key = {h.key: h for h in cands}
        valid_refs = {o.ref for o in obs}
        out: list[HypothesisOut] = []
        unknown = dropped = 0
        for entry in data.get("ranking", []):
            key = str(entry.get("id", ""))
            h = by_key.pop(key, None)
            if h is None:
                unknown += 1
                continue
            refs = [r for r in entry.get("refs", []) if r in valid_refs]
            dropped += len(entry.get("refs", [])) - len(refs)
            text = " ".join(str(entry.get("explanation", "")).split())[:600]
            out.append(h.model_copy(update={
                "rank": len(out) + 1, "ranked_by": "llm",
                "explanation": text if refs else "", "explanation_refs": refs if text else [],
            }))  # fmt: skip
        rest = rubric_order(list(by_key.values()))
        out += [h.model_copy(update={"rank": len(out) + i + 1}) for i, h in enumerate(rest)]
        if unknown or dropped:
            notes.append(f"ranking: {unknown} unknown id(s), {dropped} invalid ref(s) dropped.")
        degraded = (
            f"{len(rest)} candidate(s) ranked by rubric: model skipped them" if rest else None
        )
        return out, degraded
