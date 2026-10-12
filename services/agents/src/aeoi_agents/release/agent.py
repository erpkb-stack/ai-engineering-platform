"""Release risk agent (FR-8, ADR-025). CODE ONLY - no model. ADVISORY: nothing reads its result
to gate a deploy.

  1 target (tools, AS THE USER): a pull request -> its merge commit -> the commit's service;
    a range (a release candidate: service + what runs now + what would ship); or a deploy ->
    its commit, the previous successful deploy's commit, its config changes
  2 what it ships: compare_commits, then the Phase 14 diff rules (code/agent.scan_commit)
  3 blast radius: query_service_catalog (tier, dependents)
  4 change impact for a removed field: find_references (only code every engineer may read)
  5 rubric (release/rubric.py): factors -> band; rollback plan from flag guards + config
Every factor cites the evidence it came from. Anything not checked is a MEDIUM factor of its
own, so a report is never LOW by silence.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from aeoi_agents.code.agent import FileHit, _commit_evidence, _ref_evidence, scan_commit
from aeoi_agents.common import Failure, call
from aeoi_agents.config import ReleaseBudget
from aeoi_agents.release.rubric import (
    Hit,
    assess,
    band_of,
    findings_band,
    flag_keys,
    rollback_plan,
)
from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import AgentRunResult, EvidenceItem
from aeoi_models.api.release import ReleaseRiskReport, ReleaseRiskTask, RiskFactor
from aeoi_models.api.tools import ToolResult
from aeoi_tool_client import CallContext, ToolClient

AGENT_NAME = "release_risk"
AGENT_VERSION = "1.0.0"
MAX_EVIDENCE = 100


@dataclass
class Deps:
    tools: ToolClient
    budget: ReleaseBudget


def _item_evidence(kind: str, item: dict[str, Any], title: str, body: str,
                   tool_call_id: Any, source: str) -> EvidenceItem:  # fmt: skip
    body = body[:4000]
    return EvidenceItem(
        evidence_key=str(item["evidence_id"]), kind=kind, source_system=source,
        title=title[:300], excerpt=body, content_sha256=hashlib.sha256(body.encode()).hexdigest(),
        tool_call_id=tool_call_id,
    )  # fmt: skip


class ReleaseRiskAgent:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def run(
        self, task: ReleaseRiskTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        b = self.deps.budget
        deadline = time.monotonic() + b.task_deadline_s
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        ctx = CallContext(user_token=user_token, agent_name=AGENT_NAME,
                          task_id=task.assessment_id, correlation_id=correlation_id)  # fmt: skip
        req = task.request
        rep = ReleaseRiskReport(target_kind=req.kind, repository=req.repository,
                                pull_request=req.pull_request, deploy_key=req.deploy_key,
                                service_key=req.service_key, base_sha=req.base_sha,
                                head_sha=req.head_sha)  # fmt: skip
        evidence: dict[str, EvidenceItem] = {}

        def failed(why: str) -> AgentRunResult:
            return AgentRunResult(status="FAILED", error=why[:500], release=rep,
                                  trace=tracer.finish())  # fmt: skip

        async def tool(name: str, args: dict[str, Any]) -> ToolResult | Failure:
            """Every call is bounded by what is LEFT of the task deadline (review: only one
            check existed; a slow devdata made the orchestrator time out and record nothing)."""
            left = deadline - time.monotonic()
            if left <= 0:
                return Failure(reason="task deadline", denied=False)
            try:
                return await asyncio.wait_for(call(self.deps.tools, tracer, name, args, ctx), left)
            except TimeoutError:
                return Failure(reason="task deadline", denied=False)

        # ---- 1. resolve the target -------------------------------------------------------
        config_keys: list[str] = []
        if req.deploy_key:
            res = await tool("get_deployment", {"deploy_key": req.deploy_key})
            if isinstance(res, Failure):
                return failed(f"get_deployment: {res.reason}")
            items = res.data.get("items", [])
            if not items:
                return failed(f"{req.deploy_key} not found")
            d = items[0]
            rep.service_key, rep.head_sha = d["service_key"], d["commit_sha"]
            rep.base_sha = d.get("previous_commit_sha")
            config_keys = list(d.get("config_changed_keys") or [])
            ev = _item_evidence("DEPLOY", d, f"Deploy {d['deploy_key']} on {d['service_key']}",
                                f"{d['deploy_key']} {d['service_key']} {d['version']} ({d['status']}), "
                                f"commit {d['commit_sha'][:12]}, config changed: "
                                f"{', '.join(config_keys) or 'none'}", res.tool_call_id,
                                "devdata.deployments")  # fmt: skip
            evidence[ev.evidence_key] = ev
            undo = (f"a redeploy of {rep.base_sha[:12]} (the previous successful deploy)"
                    if rep.base_sha else "a rollback to the previous release")  # fmt: skip
            deploy_ev = ev.evidence_key
        elif req.kind == "range":
            # a release candidate: the caller says what runs now (base) and what would ship
            undo = f"not shipping {req.head_sha[:12] if req.head_sha else 'it'} (or a rollback to " \
                   f"{req.base_sha[:12] if req.base_sha else 'base'} once deployed)"  # fmt: skip
            deploy_ev = None
        else:
            res = await tool("get_pull_request",
                             {"repository": req.repository, "number": req.pull_request})  # fmt: skip
            if isinstance(res, Failure):
                return failed(f"get_pull_request: {res.reason}")
            items = res.data.get("items", [])
            if not items:
                return failed(f"{req.repository}#{req.pull_request} not found")
            pr = items[0]
            pev = _item_evidence("PR", pr, f"PR #{req.pull_request} in {req.repository}",
                                 f"{req.repository}#{req.pull_request}: {str(pr.get('title'))[:200]} "
                                 f"(merge commit {str(pr.get('merge_commit_sha'))[:12]})",
                                 res.tool_call_id, "devdata.pull_requests")  # fmt: skip
            evidence[pev.evidence_key] = pev
            if not pr.get("merge_commit_sha"):
                return failed(f"PR #{req.pull_request} has no merge commit yet: nothing to analyse "
                              "([P] the synthetic history holds merged PRs only)")  # fmt: skip
            rep.head_sha = pr["merge_commit_sha"]
            res = await tool("get_commit", {"sha": rep.head_sha})
            if isinstance(res, Failure):
                return failed(f"get_commit: {res.reason}")
            if not res.data.get("items"):
                return failed(f"merge commit {rep.head_sha[:12]} not found")
            rep.service_key = res.data["items"][0]["service_key"]
            undo = f"reverting PR #{req.pull_request} (or a rollback once deployed)"
            deploy_ev = pev.evidence_key  # nothing_shipped / config facts cite the PR
        if not (rep.service_key and rep.head_sha):
            return failed("the target has no service or commit")
        factors: list[RiskFactor] = []
        not_checked: list[str] = []

        # ---- 2. what it ships ----------------------------------------------------------
        res = await tool("compare_commits", {
            "service_key": rep.service_key, "head_sha": rep.head_sha,
            "base_sha": rep.base_sha, "limit": b.max_commits,
        })  # fmt: skip
        if isinstance(res, Failure):
            return failed(f"compare_commits: {res.reason}")
        relation = res.data.get("relation", "ahead")
        if rep.target_kind == "range" and relation == "rollback":
            # review: a candidate whose head is OLDER than what runs now - swapped arguments,
            # never a quiet "nothing ships, LOW"
            return failed(f"head {rep.head_sha[:12]} is older than base "
                          f"{(rep.base_sha or '?')[:12]}: base and head swapped?")  # fmt: skip
        if relation in ("same", "rollback"):
            factors.append(RiskFactor(kind="nothing_shipped", severity="context", statement=(
                f"{req.label()} " + ("ships the commit already running"
                                     if relation == "same" else "goes back to an older commit")
                + ": no new code ships."), evidence_ids=[deploy_ev] if deploy_ev else []))  # fmt: skip
            if config_keys:  # review: a config-only deploy - its config IS the change
                factors.append(RiskFactor(kind="config_change", severity="context",
                    statement=f"Config changed with this deploy: {', '.join(config_keys)}.",
                    evidence_ids=[deploy_ev] if deploy_ev else []))  # fmt: skip
            rep.factors, rep.band, rep.checked = factors, band_of(factors), ["code"]
            rep.findings_band = rep.band
            rep.rollback = rollback_plan([], flag_keys(config_keys), [], undo=undo,
                                         code_shipped=False)  # fmt: skip
            return AgentRunResult(status="SUCCEEDED", release=rep,
                                  evidence=list(evidence.values()), trace=tracer.finish())  # fmt: skip
        if relation == "head_unknown":
            return failed(f"commit {rep.head_sha[:12]} is not in {rep.service_key}'s history")
        if rep.target_kind == "deploy" and relation == "no_base":
            not_checked.append(
                f"the previous deploy's commit {rep.base_sha[:12]} is not in the history: only "
                "the deploy's own commit checked" if rep.base_sha else
                "no previous successful deploy: only the deploy's own commit checked"
            )  # fmt: skip
        if rep.target_kind == "range" and relation == "no_base":
            not_checked.append(f"base {rep.base_sha[:12] if rep.base_sha else '?'} is not in "
                               f"{rep.service_key}'s history: only the head commit checked")  # fmt: skip
        if res.data.get("more"):
            not_checked.append(f"more than {b.max_commits} commits: only the newest checked")
        if res.truncated:  # the gateway's output cap dropped whole commits (review)
            not_checked.append("the commit list was cut by the tool gateway's output cap")
        hits: list[tuple[FileHit, str]] = []  # (hit, commit evidence id)
        contents: list[tuple[str, str]] = []  # (path, file after) - flags_read reads source only
        for item in res.data.get("items", []):
            found, skipped, _ = scan_commit(item)
            not_checked.extend(skipped)
            contents.extend((f["path"], f["content_after"])
                            for f in item.get("files", []) if f.get("content_after"))  # fmt: skip
            for fh in found:
                ev = _commit_evidence(item, fh.excerpt, res.tool_call_id)
                evidence.setdefault(ev.evidence_key, ev)
                hits.append((fh, ev.evidence_key))

        # ---- 3. blast radius -------------------------------------------------------------
        tier: int | None = None
        dependents: list[str] | None = None
        cat_ev: str | None = None
        cat = await tool("query_service_catalog", {"service_key": rep.service_key})
        blast_checked = False
        if isinstance(cat, Failure) or not cat.data.get("items"):
            not_checked.append("blast radius (service catalog): "
                               + (cat.reason if isinstance(cat, Failure) else "service not in catalog"))  # fmt: skip
        else:
            svc = cat.data["items"][0]
            tier, dependents = int(svc["tier"]), list(svc.get("dependents") or [])
            ev = _item_evidence("CATALOG", svc, f"Catalog: {svc['key']}",
                                f"{svc['key']}: tier {tier}, {len(dependents)} dependent service(s): "
                                f"{', '.join(dependents[:20]) or 'none'}", cat.tool_call_id,
                                "catalog")  # fmt: skip
            evidence[ev.evidence_key] = ev
            cat_ev, blast_checked = ev.evidence_key, True

        # ---- 4. change impact of removed fields (IO), then the pure rule ---------------------
        rules: list[Hit] = []
        impact_done = True
        for fh, ev_id in hits:
            h = Hit(rule=fh.hit.rule, detail=fh.hit.detail, symbol=fh.symbol,
                    where=f"{fh.symbol}() in {fh.hit.container or fh.path} "
                          f"({fh.path.rsplit('/', 1)[-1]}:{fh.hit.line})",
                    guard=fh.hit.guard, evidence_id=ev_id, file=fh.path.rsplit("/", 1)[-1])  # fmt: skip
            if h.rule == "field_removed":
                await self._impact(h, rep.service_key, tool, evidence, not_checked)
                impact_done = impact_done and h.consumers is not None
            rules.append(h)
        factors, band, rollback = assess(
            rules, service_key=rep.service_key, tier=tier, dependents=dependents,
            catalog_evidence=cat_ev, config_keys=config_keys, deploy_evidence=deploy_ev,
            not_checked=not_checked, contents=contents, undo=undo,
        )  # fmt: skip
        # every evidence id a factor cites goes back with the report (review lesson, Phase 14
        # M6: a cap must never leave a citation pointing at nothing)
        # cited evidence first, so the cap can only drop what nothing cites (review)
        cited = {e for f in factors for e in f.evidence_ids}
        order = sorted(evidence, key=lambda k: k not in cited)
        stored = {k: evidence[k] for k in order[:MAX_EVIDENCE]}
        factors = [f.model_copy(update={"evidence_ids": [e for e in f.evidence_ids if e in stored]})
                   for f in factors]  # fmt: skip
        rep.factors, rep.band, rep.rollback = factors, band, rollback
        rep.findings_band = findings_band(factors)
        rep.coverage = "partial" if not_checked else "full"
        # only what WAS assessed (review: blast_radius/impact were listed even when they failed)
        rep.checked = ["code", "rollback"] + (["blast_radius"] if blast_checked else []) + (
            ["impact"] if impact_done and any(h.rule == "field_removed" for h in rules) else [])  # fmt: skip
        rep.not_checked = list(not_checked)[:20]
        return AgentRunResult(
            status="SUCCEEDED", degraded="; ".join(not_checked)[:1000] or None, release=rep,
            evidence=list(stored.values()), trace=tracer.finish(),
        )  # fmt: skip

    async def _impact(self, h: Hit, service_key: str, tool: Any,
                      evidence: dict[str, EvidenceItem], not_checked: list[str]) -> None:  # fmt: skip
        """Who else reads a removed field - only code every engineer may read counts (it is
        stored on a record people share). Fills h.consumers; None stays = not checked. A result
        that hit `max_refs` is NOT "nobody else uses it" (review: 50 references inside its own
        service pushed every outside reader past the cut) - it is a part not checked."""
        limit = self.deps.budget.max_refs
        res = await tool("find_references", {"symbol": h.symbol, "limit": limit})
        if isinstance(res, Failure):
            not_checked.append(f"impact of {h.symbol}: {res.reason}")
            return
        items = res.data.get("items", [])
        refs = [r for r in items
                if r.get("service_key") != service_key and r.get("open_to_engineering")]  # fmt: skip
        if len(items) >= limit or res.truncated:
            not_checked.append(f"impact of {h.symbol}: more than {limit} references, "
                               "other readers may be missing")  # fmt: skip
            if not refs:
                return  # unknown, not "nobody": consumers stays None
        h.consumers = sorted({r.get("service_key") or r["repository"] for r in refs})
        for r in refs[:9]:
            ev = _ref_evidence(r, h.symbol, res.tool_call_id)
            evidence.setdefault(ev.evidence_key, ev)
            h.consumer_evidence.append(ev.evidence_key)
