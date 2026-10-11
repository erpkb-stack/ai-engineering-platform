"""Historical incident agent (architecture.md §10 #8). ADR-023.

  1 observations (code): the same timeline the hypothesis agent builds
  2 query        (code): incident title + error codes + symptom words + "after deployment"
  3 search_incidents (tool -> rag, hybrid), AS THE USER, twice: this service's own history
    (service = filter, `same_service` hits) first, then the same failure anywhere (boosted)
  4 only past incidents shared with every engineer: the result is stored on THIS incident,
    where anyone who may read it sees it. The search asks rag for `shared_only` (filtered in
    SQL, never read); the agent drops any other hit again (defence in depth, counted in the
    trace only - a count of restricted matches on the investigation would itself leak).
  5 compare (LLM, Haiku): typed claims "same/different <field>" per past incident;
    code computes every relation itself and KEEPS a claim only when code agrees. Model down ->
    code's relations are used (`compared_by=code`, degraded).
  6 priors (code): "N of M similar past incidents had a cause of this kind" per cause kind.

Honest note (ADR-023): code computes every relation the model may claim, so the model adds
no information here. It is kept because the owner chose "LLM compares"; `claims_rejected` is
the measurement that decides (Phase 25) whether it earns its cost.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

from aeoi_agents.common import Failure, call
from aeoi_agents.config import HistoryBudget
from aeoi_agents.prompts import Prompt, load_prompt
from aeoi_agents.reasoning import llm as llm_call
from aeoi_agents.reasoning.hypothesis import observation_block
from aeoi_agents.reasoning.observations import build, clean, first_effect
from aeoi_agents.trace import Tracer
from aeoi_llm_client import LLMClient
from aeoi_models.api.agents import AgentRunResult, EvidenceItem
from aeoi_models.api.history import (
    Claim,
    HistoryPrior,
    HistoryStats,
    SimilarIncident,
    Symptom,
)
from aeoi_models.api.hypotheses import Observation
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_security.untrusted import wrap_untrusted
from aeoi_tool_client import CallContext, ToolClient

AGENT_NAME = "historical_incident"
AGENT_VERSION = "1.0.0"
PROMPT_VERSION = 1
SOURCE = "rag.historical_incidents"
SYMPTOMS: tuple[str, ...] = get_args(Symptom)

# what this incident shows (metric observations) <-> what a past write-up says (its text)
SYMPTOM_METRICS: dict[str, set[str]] = {
    "errors": {"http_5xx_per_min"},
    "latency": {"p95_latency_ms", "db_query_latency_ms"},
    "connection_pool": {"db_pool_utilization", "db_connections_active"},
    "thread_pool": {"thread_pool_utilization"},
    "cache": {"cache_hit_ratio"},
    "cpu": {"cpu_util"},
}
SYMPTOM_TEXT: dict[str, re.Pattern[str]] = {
    "errors": re.compile(r"\b5xx\b|\bhttp 5\d\d\b|\berrors?\b", re.I),
    "latency": re.compile(r"latency|slow", re.I),
    "connection_pool": re.compile(r"connection (pool|acquisition)|exhausting the pool", re.I),
    "thread_pool": re.compile(r"thread pool|executor|request queue", re.I),
    "cache": re.compile(r"\bcache\b", re.I),
    "cpu": re.compile(r"\bcpu\b", re.I),
}
QUERY_WORDS = {
    "http_5xx_per_min": "HTTP 5xx errors",
    "p95_latency_ms": "latency",
    "db_query_latency_ms": "database query latency",
    "db_pool_utilization": "database connection pool",
    "db_connections_active": "database connections",
    "thread_pool_utilization": "thread pool",
    "cache_hit_ratio": "cache hit rate",
    "cpu_util": "CPU",
    "requests_per_sec": "traffic",
}
# what came BEFORE the past incident is in its title/summary; root cause and remediation say
# why and how it was fixed ("Rolled back the deployment" is the fix, not the trigger: review)
DEPLOY_TEXT = re.compile(r"deploy|release|new version", re.I)
NO_DEPLOY_TEXT = re.compile(r"\bno (code )?(deploy|release)", re.I)
ERROR_CODE_TEXT = re.compile(r"\bERR_[A-Z0-9_]{2,60}\b")
# Which candidate cause kinds a past root-cause CATEGORY speaks for. The synthetic vocabulary
# has 12 categories; a real org would keep this table with its incident taxonomy. A category
# with no kind (certificate expiry, DNS, ...) counts in M but supports no candidate.
CATEGORY_KINDS: dict[str, tuple[str, ...]] = {
    "connection_pool_exhaustion": ("change", "saturation"),
    "thread_pool_saturation": ("saturation",),
    "memory_leak": ("saturation",),
    "bad_config_change": ("change",),
    "schema_migration_lock": ("change",),
    "feature_flag_regression": ("change",),
    "cache_stampede": ("traffic",),
}
KIND_WORDS = {
    "change": "a deploy or config change",
    "saturation": "resource saturation",
    "traffic": "a traffic change",
}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "comparisons": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "incident": {"type": "string", "pattern": r"^INC-[0-9]{1,12}$"},
                    "claims": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {
                                    "type": "string",
                                    "enum": ["service", "error_code", "symptom", "deploy_before"],
                                },
                                "symptom": {"type": "string", "enum": ["", *SYMPTOMS]},
                                "relation": {"type": "string", "enum": ["same", "different"]},
                            },
                            "required": ["field", "symptom", "relation"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["incident", "claims"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comparisons"],
    "additionalProperties": False,
}


@dataclass
class Deps:
    tools: ToolClient
    llm: LLMClient
    budget: HistoryBudget


# ------------------------------------------------------------------ pure helpers (tested)
def now_codes(obs: list[Observation]) -> set[str]:
    return {o.subject for o in obs if o.agent == "log_analysis" and o.role == "effect"}


def now_symptoms(obs: list[Observation]) -> set[str]:
    subjects = {o.subject for o in obs if o.role in ("effect", "resource")}
    return {s for s, metrics in SYMPTOM_METRICS.items() if subjects & metrics}


def deploy_before_now(obs: list[Observation]) -> bool:
    first = first_effect(obs)
    return first is not None and any(o.role == "change" and o.at <= first.at for o in obs)


def build_query(title: str | None, obs: list[Observation]) -> str:
    """Code-built: the title is user input, used ONLY as search text (never in a prompt)."""
    parts = [" ".join((title or "").split())]
    parts += sorted(now_codes(obs))[:3]
    words: list[str] = []
    for o in obs:
        w = QUERY_WORDS.get(o.subject)
        if o.role in ("effect", "resource", "traffic") and w and w not in words:
            words.append(w)
    parts += words[:5]
    if deploy_before_now(obs):
        parts.append("after deployment")
    return " ".join(p for p in parts if p).strip()[:300] or "incident"


def hit_text(hit: dict[str, Any]) -> str:
    """What the past incident LOOKED like (title + summary), not its cause or fix."""
    return " ".join(str(hit.get(f) or "") for f in ("title", "summary"))


def deploy_before_then(hit: dict[str, Any]) -> bool:
    text = hit_text(hit)
    return bool(DEPLOY_TEXT.search(text)) and not NO_DEPLOY_TEXT.search(text)


def _sides(now: bool, then: bool) -> str:
    return {(True, True): "both", (False, False): "neither", (True, False): "this incident only",
            (False, True): "past incident only"}[(now, then)]  # fmt: skip


def relations(
    hit: dict[str, Any], obs: list[Observation], services: set[str]
) -> dict[tuple[str, str], tuple[str, str]]:
    """Every relation code can CHECK: (field, symptom) -> (relation, code-written value).
    A pair absent here cannot be checked (e.g. neither side names an error code). The value
    says which side has it, so "same" never reads as "both had it" when neither did."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    text = hit_text(hit)
    shared_svc = sorted(set(hit.get("service_keys") or []) & services)
    out[("service", "")] = ("same", shared_svc[0]) if shared_svc else ("different", "")
    now_c = now_codes(obs)
    then_c = set(ERROR_CODE_TEXT.findall(str(hit.get("summary") or "")))
    if now_c and then_c:
        both = sorted(now_c & then_c)
        out[("error_code", "")] = ("same", both[0]) if both else ("different", "")
    now_s = now_symptoms(obs)
    for s in SYMPTOMS:
        a, b = s in now_s, bool(SYMPTOM_TEXT[s].search(text))
        if a or b:
            out[("symptom", s)] = ("same" if a and b else "different", f"{s}: {_sides(a, b)}")
    now_d, then_d = deploy_before_now(obs), deploy_before_then(hit)
    out[("deploy_before", "")] = ("same" if now_d == then_d else "different", _sides(now_d, then_d))
    return out


def check_claims(
    proposed: list[dict[str, Any]], facts: dict[tuple[str, str], tuple[str, str]]
) -> tuple[list[Claim], int]:
    """Keep a claim only if code computes the same relation; duplicates count once."""
    kept: dict[tuple[str, str], Claim] = {}
    rejected = 0
    for c in proposed:
        field = str(c.get("field", ""))
        sym = str(c.get("symptom", "")) if field == "symptom" else ""
        fact = facts.get((field, sym))
        if fact is None or fact[0] != c.get("relation"):
            rejected += 1
            continue
        kept.setdefault(
            (field, sym),
            Claim(field=field, relation=fact[0], value=fact[1]),  # type: ignore[arg-type]
        )
    return list(kept.values()), rejected


def code_claims(facts: dict[tuple[str, str], tuple[str, str]]) -> list[Claim]:
    return [
        Claim(field=f, relation=r, value=v)  # type: ignore[arg-type]
        for (f, _s), (r, v) in sorted(facts.items())
    ][:8]


def priors(similar: list[SimilarIncident]) -> list[HistoryPrior]:
    m = len(similar)
    out: list[HistoryPrior] = []
    for kind in ("change", "saturation", "traffic"):
        hits = [s for s in similar if kind in CATEGORY_KINDS.get(s.root_cause_category, ())]
        if not hits:
            continue
        cats = Counter(s.root_cause_category for s in hits)
        keys = [s.incident_key for s in hits][:10]
        out.append(
            HistoryPrior(
                cause_kind=kind,
                n=len(hits),
                of=m,
                categories=dict(cats),
                incident_keys=keys,
                statement=(
                    f"{len(hits)} of {m} similar past incidents (shared with all engineers) "
                    f"had {KIND_WORDS[kind]} as root cause: "
                    + ", ".join(f"{c} x{n}" for c, n in cats.most_common())
                    + f" ({', '.join(keys[:5])})."
                )[:300],
            )
        )
    return out


def _pointer(s: SimilarIncident, tool_call_id: Any) -> EvidenceItem:
    """No text from the past incident: its key, category and rank only (ADR-023)."""
    excerpt = (
        f"past incident {s.incident_key} ({s.root_cause_category}), similar rank {s.rank} "
        f"({s.retrieval})"
    )
    return EvidenceItem(
        evidence_key=s.evidence_id,
        kind="INCIDENT",
        source_system=SOURCE,
        title=f"Similar past incident {s.incident_key}",
        excerpt=excerpt,
        content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        observed_at=s.occurred_at,
        tool_call_id=tool_call_id,
        uri=f"rag://incidents/{s.incident_key}",
    )


def past_block(hits: list[dict[str, Any]]) -> str:
    body = "\n".join(
        f"{h['incident_key']} services={','.join(h.get('service_keys') or [])} "
        f"title={clean(str(h.get('title') or ''), 160)} "
        f"summary={clean(str(h.get('summary') or ''), 300)} "
        f"root_cause={clean(str(h.get('root_cause') or ''), 300)}"
        for h in hits
    )
    return wrap_untrusted(body, source="rag.historical_incidents", item_id="past", max_chars=8_000)


class HistoryAgent:
    def __init__(self, deps: Deps, prompt: Prompt | None = None) -> None:
        self.deps = deps
        self.prompt = prompt or load_prompt("history", PROMPT_VERSION)

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
        services = set(task.service_keys) or {o.service_key for o in obs}
        query = build_query(task.incident_title, obs)
        ctx = CallContext(
            user_token=user_token, agent_name=AGENT_NAME, incident_id=task.incident_id,
            investigation_id=task.investigation_id, task_id=task.task_id,
            correlation_id=correlation_id,
        )  # fmt: skip
        # Two lists, no weight to tune (Mac smoke 2026-10-11: with ~90 identical write-ups per
        # category, vector noise beat a half-vote service boost and NO incident of the demo's own
        # service came back). 1) this service's own history (service = FILTER); 2) the same
        # failure anywhere (service = boost). Own-service hits first, then the rest.
        base: dict[str, Any] = {"query": query, "mode": "hybrid", "shared_only": True}
        svc = sorted(task.service_keys)[0] if task.service_keys else None
        calls: list[tuple[str, dict[str, Any]]] = []
        if svc is not None and b.same_service > 0:
            calls.append(("service", {**base, "k": b.same_service, "service_key": svc}))
        calls.append(("all", {**base, "k": b.k, "service_key": svc, "boost_service": True}))
        items: list[dict[str, Any]] = []
        failed: list[str] = []
        seen: set[str] = set()
        for scope, args in calls:
            res = await call(self.deps.tools, tracer, "search_incidents", args, ctx)
            if isinstance(res, Failure):
                failed.append(f"search_incidents ({scope}): {res.reason}")
                continue
            for h in res.data.get("items", []):
                if h["incident_key"] not in seen:  # a twin found by both lists counts once
                    seen.add(h["incident_key"])
                    items.append({**h, "_scope": scope, "_call": res.tool_call_id})
        if len(failed) == len(calls):
            return AgentRunResult(status="FAILED", error="; ".join(failed), trace=tracer.finish())
        shared = [h for h in items if h.get("open_to_engineering")][: b.max_compared]
        stats = HistoryStats(
            searched=len(items),
            not_shared=sum(1 for h in items if not h.get("open_to_engineering")),
            retrieval=(items[0].get("retrieval") if items else None),
        )
        notes: list[str] = [f"query (code): {query[:200]}"]
        if stats.not_shared:
            notes.append(
                f"{stats.not_shared} matching past incident(s) are not shared with all "
                "engineers: not stored here and not used."
            )
        if stats.retrieval == "keyword":
            notes.append("vector search unavailable: keyword search only.")
        if not shared:
            notes.append("no similar past incident shared with all engineers.")
            return AgentRunResult(
                status="SUCCEEDED",
                notes=notes,
                history=stats,
                trace=tracer.finish(),
                degraded="; ".join(failed) or None,
            )
        facts = {h["incident_key"]: relations(h, obs, services) for h in shared}
        user = (
            "THIS INCIDENT - OBSERVATIONS (time order)\n" + observation_block(obs)
            + "\n\nPAST INCIDENTS\n" + past_block(shared)
            + "\n\nCompare each past incident with this incident."
        )  # fmt: skip
        data, error = await llm_call.structured(
            self.deps.llm, tracer, self.prompt, user, SCHEMA,
            agent=AGENT_NAME, route=task.llm_route or b.route, max_tokens=b.max_tokens,
            timeout_s=max(1.0, min(b.llm_timeout_s, deadline - time.monotonic() - 5)),
            use_cache=task.llm_cache, investigation_id=task.investigation_id,
            schema_name="history_comparison",
        )  # fmt: skip
        proposed: dict[str, list[dict[str, Any]]] = {}
        unknown = 0
        for cmp_ in (data or {}).get("comparisons", []):
            key = str(cmp_.get("incident", ""))
            if key not in facts:
                unknown += 1
                continue
            proposed.setdefault(key, []).extend(cmp_.get("claims", []))
        similar: list[SimilarIncident] = []
        for i, h in enumerate(shared, 1):
            key = h["incident_key"]
            if data is not None:
                claims, rejected = check_claims(proposed.get(key, []), facts[key])
                stats.claims_proposed += len(proposed.get(key, []))
                stats.claims_kept += len(claims)
                stats.claims_rejected += rejected
                by = "llm"
            else:
                claims, by = code_claims(facts[key]), "code"
            similar.append(
                SimilarIncident(
                    incident_key=key, rank=i, retrieval=h.get("retrieval") or "keyword",
                    rank_keyword=h.get("rank_k"), rank_vector=h.get("rank_v"),
                    root_cause_category=str(h.get("root_cause_category") or "unknown")[:64],
                    service_keys=list(h.get("service_keys") or [])[:10],
                    occurred_at=_dt(h["occurred_at"]), evidence_id=str(h["evidence_id"]),
                    claims=claims[:8], compared_by=by,  # type: ignore[arg-type]
                    scope=h["_scope"],
                )
            )  # fmt: skip
        if unknown:
            notes.append(f"comparison: {unknown} unknown incident id(s) dropped.")
        if data is not None:
            notes.append(
                f"claims: {stats.claims_kept} kept, {stats.claims_rejected} rejected by code."
            )
        problems = failed + ([f"compared by code: {error}"] if error else [])
        calls_by_key = {h["incident_key"]: h["_call"] for h in shared}
        return AgentRunResult(
            status="SUCCEEDED",
            degraded="; ".join(problems) or None,
            notes=notes[:30],
            similar=similar,
            priors=priors(similar),
            history=stats,
            evidence=[_pointer(s, calls_by_key[s.incident_key]) for s in similar],
            trace=tracer.finish(),
        )


def _dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
