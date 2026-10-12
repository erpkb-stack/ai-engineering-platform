"""Postmortem agent (architecture.md §9 'report'; FR-5, FR-13). ADR-022.

1 observations (code): rebuilt from the evidence agents' results (same refs as the ranker:
                       the orchestrator passes the ranker's observations_sha; mismatch = refuse)
2 timeline     (code): every non-steady observation + the detection time, as TimelinePoints;
                       steady observations become "did not change" (negative evidence)
3 write        (LLM, owner decision: Sonnet = `reasoning`, fallback OFF): prose sections and
                       proposed action items over the VALIDATED hypotheses it is given
4 check        (code): a sentence must cite >= 1 existing observation ref, and every number
                       in it must occur in the input it was shown; otherwise it is dropped
                       and counted. The model never writes the root cause, a band, a status,
                       a time-to-detect or any header number: incident-service computes those.
5 degrade:     LLM down -> timeline + negative evidence only, written_by="code", degraded
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aeoi_agents.common import PROSE_MAX, clip, hhmm
from aeoi_agents.config import PostmortemBudget
from aeoi_agents.prompts import Prompt, load_prompt
from aeoi_agents.reasoning import llm as llm_call
from aeoi_agents.reasoning.observations import build
from aeoi_agents.trace import Tracer
from aeoi_llm_client import LLMClient
from aeoi_models.api.agents import AgentRunResult
from aeoi_models.api.hypotheses import HypothesisOut, Observation, observations_fingerprint
from aeoi_models.api.postmortem import (
    ActionItem,
    NoChange,
    PmSection,
    PmSentence,
    PostmortemDraft,
    TimelinePoint,
)
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_security.untrusted import wrap_untrusted

AGENT_NAME = "postmortem"
AGENT_VERSION = "1.0.0"
PROMPT_VERSION = 1
REF = {"type": "string", "pattern": r"^o[0-9]{1,3}$"}
SECTIONS = ("summary", "what_happened", "why", "detection", "went_well", "went_poorly")
MAX_SENTENCES = 8
SENTENCE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "maxLength": PROSE_MAX},
        "refs": {"type": "array", "maxItems": 4, "items": REF},
    },
    "required": ["text", "refs"],
    "additionalProperties": False,
}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{s: {"type": "array", "maxItems": MAX_SENTENCES, "items": SENTENCE} for s in SECTIONS},
        "action_items": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": PROSE_MAX},
                    "category": {"type": "string",
                                 "enum": ["prevent", "detect", "mitigate", "process"]},
                    "refs": {"type": "array", "maxItems": 4, "items": REF},
                },
                "required": ["text", "category", "refs"],
                "additionalProperties": False,
            },
        },
    },
    "required": [*SECTIONS, "action_items"],
    "additionalProperties": False,
}  # fmt: skip
# Numbers (review: "900ms" and "3x" slipped past a number-then-no-letter pattern, and the whole
# prompt - dates, z-scores, the user's title - made almost any small number "known"):
# - extracted wherever digits start a token, even glued to a unit ("900ms" -> 900); a digit
#   inside a name ("p95", "http_5xx") is not a number (preceded by a letter or "_")
# - compared as TOKENS against a corpus of code-written facts only: the cited observations
#   (statement + time), the computed numbers and the validated hypotheses; dates are taken
#   out first, so "12" is not "known" just because a date says 2026-10-12
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
NUMBER = re.compile(r"(?<![A-Za-z_.\d])\d+(?:[.:,]\d+)*")
# model text that a markdown renderer would turn into a link, image or HTML (a beacon)
MARKUP = re.compile(r"://|\]\(|<|>|!\[|www\.", re.I)
SOURCES = {"DEPLOY", "CONFIG", "ALERT", "LOG", "METRIC", "TRACE"}


@dataclass
class Deps:
    llm: LLMClient
    budget: PostmortemBudget


def _hhmm(d: datetime) -> str:
    # astimezone: results come back from the orchestrator's store with the DB session's offset
    # (-05:00 in the sandbox); a "Z" on a non-UTC time was a live-run bug (smoke 12)
    return hhmm(d)


def timeline(obs: list[Observation], detected_at: datetime | None) -> list[TimelinePoint]:
    """CODE: every observation that is an event (not 'stayed steady') + detection."""
    points = []
    for o in obs:
        if o.role in ("steady", "code"):  # code: a fact about the deploy, not an event in time
            continue
        kind = o.evidence[0].kind.value if o.evidence else "AGENT"
        points.append(TimelinePoint(
            ref=o.ref, at=o.at.astimezone(UTC), source=kind if kind in SOURCES else "AGENT",  # type: ignore[arg-type]
            observed_by=o.agent,
            event_type=f"OBSERVED_{o.role.upper()}", service_key=o.service_key,
            subject=o.subject, statement=o.statement[:500],
            evidence_keys=[e.evidence_id for e in o.evidence][:4], sustained=o.sustained,
        ))  # fmt: skip
    if detected_at is not None:
        points.append(TimelinePoint(
            at=detected_at.astimezone(UTC), source="SYSTEM", event_type="INCIDENT_DETECTED",
            subject="detected_at", observed_by="postmortem",
            statement=f"Incident detected at {_hhmm(detected_at)} (detected_at on the incident).",
        ))  # fmt: skip
    return sorted(points, key=lambda p: p.at)[:60]


def trigger_ref(obs: list[Observation], hs: list[HypothesisOut]) -> str | None:
    """CODE: the observation of the top hypothesis' cause, if it is a code-proposed change
    (a deploy). Never a critic alternative's refs (model-chosen)."""
    live = [h for h in hs if h.status != "REJECTED"]
    top = min(live, key=lambda h: h.rank) if live else None
    refs = {o.ref for o in obs}
    if top and top.origin == "code" and top.cause_kind == "change" and top.supporting \
            and top.supporting[0] in refs:  # fmt: skip
        return top.supporting[0]
    return None


def facts_block(
    obs: list[Observation], hs: list[HypothesisOut], detected_at: datetime | None
) -> str:
    """Numbers CODE computed, so the model can use them without inventing any."""
    lines = []
    effects = [o for o in obs if o.role == "effect"]
    sustained = [o for o in effects if o.sustained] or effects
    first = min(sustained, key=lambda o: o.at) if sustained else None
    if first:
        lines.append(f"first symptom: {first.subject} at {_hhmm(first.at)} ({first.ref})")
        if detected_at:
            m = round((detected_at - first.at).total_seconds() / 60, 1)
            lines.append(f"minutes from first symptom to detection: {m:g}")
    trigger = trigger_ref(obs, hs)
    if trigger and first:
        cause = {o.ref: o for o in obs}[trigger]
        if cause:
            m = round((first.at - cause.at).total_seconds() / 60, 1)
            lines.append(f"minutes from {cause.subject} start to first symptom: {m:g}")
    return "\n".join(lines) or "(none)"


def timeline_block(obs: list[Observation]) -> str:
    """Like the ranker's observation block, plus each observation's time: the narrative is a
    sequence, and a time the model may use must be in its input (the number check)."""
    body = "\n".join(f"{o.ref} {_hhmm(o.at)} [{o.role}] {o.statement}" for o in obs)
    return wrap_untrusted(body, source="evidence-agents", item_id="observations", max_chars=12_000)


def hypotheses_block(hs: list[HypothesisOut], ruled: list[str]) -> str:
    """The VALIDATED result as code stored it. The critic's refinement is model text: marked."""
    out = []
    for h in sorted(hs, key=lambda h: h.rank):
        out.append(f"{h.key} [{h.confidence.value}, {h.status}] {h.statement} "
                   f"support={','.join(h.supporting)}")  # fmt: skip
        if h.refinement:
            out.append(f"   critic's proposed mechanism (model text): {h.refinement}")
    out += [f"ruled out: {r}" for r in ruled]
    return wrap_untrusted("\n".join(out) or "(no hypothesis survived validation)",
                          source="validated-hypotheses", item_id="hypotheses", max_chars=8_000)  # fmt: skip


class PostmortemAgent:
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
        if task.observations_sha and task.observations_sha != observations_fingerprint(obs):
            return AgentRunResult(
                status="FAILED", trace=tracer.finish(),
                error="observation mismatch: rebuilt different evidence than the ranker",
            )  # fmt: skip
        points = timeline(obs, task.detected_at)
        no_change = [
            NoChange(text=o.statement[:300], evidence_keys=[e.evidence_id for e in o.evidence][:4])
            for o in obs if o.role == "steady"
        ][:12]  # fmt: skip
        draft = PostmortemDraft(
            timeline=points, no_change=no_change, trigger_ref=trigger_ref(obs, task.hypotheses)
        )
        if not points:
            return AgentRunResult(
                status="SUCCEEDED", observations=obs, postmortem=draft,
                degraded="nothing to write: no observation", trace=tracer.finish(),
            )  # fmt: skip
        ruled = [r.statement for r in task.ruled_out]
        facts = facts_block(obs, task.hypotheses, task.detected_at)
        title = wrap_untrusted(task.incident_title or "(untitled)", source="incident",
                               item_id="title", max_chars=300)  # fmt: skip
        user = (
            "INCIDENT\n" + title
            + "\n\nOBSERVATIONS (time order, written by code)\n" + timeline_block(obs)
            + "\n\nVALIDATED HYPOTHESES (code)\n" + hypotheses_block(task.hypotheses, ruled)
            + "\n\nNUMBERS COMPUTED BY CODE\n" + facts
            + "\n\nWrite the postmortem sections and action items."
        )  # fmt: skip
        data, error = await llm_call.structured(
            self.deps.llm, tracer, self.prompt, user, SCHEMA,
            agent=AGENT_NAME, route=b.route, max_tokens=b.max_tokens,
            allow_fallback=b.allow_fallback,
            timeout_s=max(1.0, min(b.llm_timeout_s, deadline - time.monotonic() - 5)),
            use_cache=task.llm_cache, investigation_id=task.investigation_id,
            schema_name="postmortem",
        )  # fmt: skip
        if data is None:
            return AgentRunResult(
                status="SUCCEEDED", observations=obs, postmortem=draft,
                degraded=f"narrative not written: {error}", trace=tracer.finish(),
            )  # fmt: skip
        draft = check(draft, obs, data, facts=facts, hypotheses=task.hypotheses)
        draft = draft.model_copy(update={
            "model": tracer.trace.model, "prompt_version": self.prompt.version,
        })  # fmt: skip
        degraded = None
        if draft.written_by == "code":  # the model answered, but nothing survived the checks
            degraded = (f"narrative empty: model wrote no usable sentence "
                        f"({draft.sentences_dropped} uncited, {draft.numbers_rejected} bad number)")  # fmt: skip
        return AgentRunResult(
            status="SUCCEEDED", degraded=degraded, observations=obs, postmortem=draft,
            trace=tracer.finish(),
        )  # fmt: skip


def _norm(n: str) -> set[str]:
    """Spellings that mean the same number: "1,840"=1840, "37.0"=37, "09:49"="9:49"."""
    n = n.replace(",", "")
    if ":" in n:
        parts = [str(int(p)) for p in n.split(":")]
        return {":".join(parts[:i]) for i in range(2, len(parts) + 1)}  # hh:mm of hh:mm:ss
    out = {n}
    try:
        f = float(n)
        out.add(f"{f:g}")
    except ValueError:
        pass
    return out


def numbers_in(text: str) -> tuple[set[str], set[str]]:
    """(dates, numbers) of a text, numbers normalised."""
    dates = set(DATE.findall(text))
    nums: set[str] = set()
    for n in NUMBER.findall(DATE.sub(" ", text)):
        nums |= _norm(n)
    return dates, nums


WORD = re.compile(r"(?<![A-Za-z_.\d])\d[\w.:,]*")


def unknown_number(text: str, known: tuple[set[str], set[str]], names: str = "") -> bool:
    """True if `text` holds a date or number code did not give the model. A digit-led WORD that
    occurs in the input as written ("5xx" from http_5xx_per_min) is a name, not a quantity."""
    dates, _ = numbers_in(text)
    if dates - known[0]:
        return True
    plain = DATE.sub(" ", text)
    for word in WORD.findall(plain):
        if (
            names
            and re.search(r"[A-Za-z]", word)
            and re.search(rf"(?<![A-Za-z\d]){re.escape(word)}(?![A-Za-z\d])", DATE.sub(" ", names))
        ):
            continue
        if any(not (_norm(n) & known[1]) for n in NUMBER.findall(word)):
            return True
    return False


def known_numbers(
    obs: list[Observation], refs: list[str], facts: str, hs: list[HypothesisOut]
) -> tuple[set[str], set[str]]:
    by_ref = {o.ref: o for o in obs}
    parts = [facts, *(h.statement for h in hs)]
    parts += [f"{_hhmm(by_ref[r].at)} {by_ref[r].statement}" for r in refs if r in by_ref]
    return numbers_in("\n".join(parts))


def check(
    draft: PostmortemDraft,
    obs: list[Observation],
    data: dict[str, Any],
    facts: str = "",
    hypotheses: list[HypothesisOut] | None = None,
) -> PostmortemDraft:
    """CODE: keep a model sentence only if it cites >= 1 existing observation that has
    evidence, holds no link/markup, and every number in it occurs in code-written facts it
    could rest on (its cited observations, the computed numbers, the validated hypotheses)."""
    by_ref = {o.ref: o for o in obs}
    hs = hypotheses or []
    names = "\n".join(f"{o.subject} {o.statement}" for o in obs)  # vocabulary, not quantities
    dropped = numbers = 0

    def keep(raw: dict[str, Any]) -> tuple[str, list[str], list[str]] | None:
        nonlocal dropped, numbers
        text = clip(raw.get("text", ""), 400)
        refs = list(dict.fromkeys(r for r in raw.get("refs", []) if r in by_ref))[:4]
        keys = list(dict.fromkeys(e.evidence_id for r in refs for e in by_ref[r].evidence))
        if len(text) < 3 or not refs or not keys or MARKUP.search(text):
            dropped += 1
            return None
        if unknown_number(text, known_numbers(obs, refs, facts, hs), names):
            numbers += 1
            return None
        return text, refs, keys[:16]

    sections = []
    for name in SECTIONS:
        kept = []
        for raw in data.get(name, [])[:MAX_SENTENCES]:
            k = keep(raw)
            if k:
                kept.append(PmSentence(text=k[0], refs=k[1], evidence_keys=k[2]))
        sections.append(PmSection(name=name, sentences=kept))  # type: ignore[arg-type]
    actions = []
    for raw in data.get("action_items", [])[:8]:
        k = keep(raw)
        if k and raw.get("category") in ("prevent", "detect", "mitigate", "process") \
                and len(k[0]) >= 5:  # fmt: skip
            actions.append(ActionItem(text=clip(k[0], 300), category=raw["category"], refs=k[1],
                                      evidence_keys=k[2]))  # fmt: skip
    written = any(s.sentences for s in sections) or bool(actions)
    return draft.model_copy(update={
        "sections": sections, "action_items": actions, "written_by": "llm" if written else "code",
        "sentences_dropped": dropped, "numbers_rejected": numbers,
    })  # fmt: skip
