"""Code intelligence agent (architecture.md §10 #5). ADR-024. CODE ONLY - no model.

  1 deploys to check (code): the deployment agent's changes that started no later than the
    first symptom (all of them when there is no symptom yet), newest first, at most 3
  2 compare_commits (tool, as the user): the commits each deploy shipped - after the previous
    successful deploy's commit, up to its own - with their diffs
  3 rules (aeoi_codeparse, tree-sitter): a database/remote call ADDED inside a loop, a numeric
    limit (timeout, retries, pool...) that changed, a data field removed or renamed
  4 change impact: for a removed field, find_references (tool, as the user, rag applies the
    user's groups in SQL) - which other services still read the old name. Only references in
    files every engineer may read are kept: they are stored on an incident (visible per ROLE)
Anything not fully checked (deadline, a file too long or not matching its diff, more commits
than the limit, an impact lookup skipped) is `degraded`, so the run is PARTIAL, never a silent
"no finding".
Every statement is a code template; every finding cites the COMMIT evidence of its commit and
every impact reference cites CODE evidence. Owner decision (ADR-024): no LLM reads the diff.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aeoi_codeparse as cp

from aeoi_agents.common import Failure, call
from aeoi_agents.config import CodeBudget
from aeoi_agents.reasoning.observations import build, first_effect
from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import AgentRunResult, DeployChange, EvidenceItem
from aeoi_models.api.code import CodeFinding, CodeStats, ImpactRef
from aeoi_models.api.reasoning import ReasoningTask
from aeoi_models.findings import EvidenceKind, EvidenceRef, Fact
from aeoi_tool_client import CallContext, ToolClient

AGENT_NAME = "code_intelligence"
AGENT_VERSION = "1.0.0"
MAX_DEPLOYS = 3
EXCERPT = 2000


@dataclass
class Deps:
    tools: ToolClient
    budget: CodeBudget


def deploys_to_check(task: ReasoningTask) -> list[DeployChange]:
    d = task.results.get("deployment")
    if d is None or d.status != "SUCCEEDED":
        return []
    first = first_effect(build(task.results))
    changes = [c for c in d.changes if first is None or c.started_at <= first.at]
    return sorted(changes, key=lambda c: c.started_at, reverse=True)[:MAX_DEPLOYS]


MAX_EVIDENCE = 100  # AgentRunResult.evidence cap
MAX_REFS_PER_FINDING = 10  # stored references per removed field (statement counts them all)


def hunk_excerpt(patch: str, line: int, removed: str | None = None) -> str:
    """The hunk that holds new-side `line`, or (a removal: line 0) the hunk that removes the
    word `removed`, else the first hunk; capped: what a reader needs."""
    hunks: list[list[str]] = []
    for ln in patch.splitlines():
        if ln.startswith("@@"):
            hunks.append([ln])
        elif hunks:
            hunks[-1].append(ln)
    for h in hunks:
        m = cp.HUNK.match(h[0])
        if m and line:
            start, count = int(m.group(3)), int(m.group(4) or 1)
            if start <= line < start + count:
                return "\n".join(h)[:EXCERPT]
        if removed and any(x.startswith("-") and re.search(rf"\b{re.escape(removed)}\b", x)
                           for x in h[1:]):  # fmt: skip
            return "\n".join(h)[:EXCERPT]
    return "\n".join(hunks[0])[:EXCERPT] if hunks else ""


def _commit_evidence(item: dict[str, Any], excerpt: str, tool_call_id: Any) -> EvidenceItem:
    body = (
        f"commit {item['sha'][:12]} by {item['author']} ({item['repository']}"
        + (f", PR #{item['pull_request']}" if item.get("pull_request") else "")
        + f"): {item['message'][:200]}\n{excerpt}"
    )[:4000]
    return EvidenceItem(
        evidence_key=item["evidence_id"], kind="COMMIT", source_system="devdata.commits",
        title=f"Commit {item['sha'][:12]} on {item['service_key']}"[:300], excerpt=body,
        content_sha256=hashlib.sha256(body.encode()).hexdigest(), observed_at=_dt(item["committed_at"]),
        tool_call_id=tool_call_id, uri=f"git://{item['repository']}/commit/{item['sha']}",
    )  # fmt: skip


def _ref_evidence(r: dict[str, Any], symbol: str, tool_call_id: Any) -> EvidenceItem:
    body = f"{r['repository']}/{r['path']}:{r['line']} {r['kind']} {r['name']} in {r.get('enclosing') or '-'} (uses {symbol})"
    return EvidenceItem(
        evidence_key=r["evidence_id"], kind="CODE", source_system="rag.code_refs",
        title=f"Use of {symbol} in {r.get('service_key') or r['repository']}"[:300], excerpt=body[:4000],
        content_sha256=hashlib.sha256(body[:4000].encode()).hexdigest(), tool_call_id=tool_call_id,
        uri=f"git://{r['repository']}/blob/HEAD/{r['path']}#L{r['line']}",
    )  # fmt: skip


class CodeAgent:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def run(
        self, task: ReasoningTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        b = self.deps.budget
        deadline = time.monotonic() + b.task_deadline_s
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        ctx = CallContext(
            user_token=user_token, agent_name=AGENT_NAME, incident_id=task.incident_id,
            investigation_id=task.investigation_id, task_id=task.task_id,
            correlation_id=correlation_id,
        )  # fmt: skip
        stats = CodeStats()
        notes: list[str] = []
        findings: list[CodeFinding] = []
        evidence: dict[str, EvidenceItem] = {}
        failures: list[str] = []
        partial: list[str] = []  # checked, but not all of it -> degraded (PARTIAL)
        deploys = deploys_to_check(task)
        if not deploys:
            notes.append("no deploy started before the first symptom: no code to check.")
            return AgentRunResult(
                status="SUCCEEDED", notes=notes, code=stats, trace=tracer.finish()
            )
        for dep in deploys:
            if time.monotonic() > deadline:
                partial.append(f"{dep.deploy_key} not checked (task deadline)")
                continue
            args = {"service_key": dep.service_key, "head_sha": dep.commit_sha,
                    "base_sha": dep.previous_commit_sha, "limit": b.max_commits}  # fmt: skip
            res = await call(self.deps.tools, tracer, "compare_commits", args, ctx)
            if isinstance(res, Failure):
                failures.append(f"compare_commits {dep.deploy_key}: {res.reason}")
                continue
            stats.deploys_checked += 1
            relation = res.data.get("relation", "ahead")
            if relation in ("same", "rollback"):
                notes.append(f"{dep.deploy_key}: " + (
                    "a redeploy of the commit already running - no new code shipped."
                    if relation == "same" else
                    "a rollback to an older commit - no new code shipped."))  # fmt: skip
                continue
            if relation == "head_unknown":
                partial.append(f"{dep.deploy_key}: its commit is not in the code history")
                continue
            if relation == "no_base" or not res.data.get("base_found", True):
                stats.base_missing += 1
                notes.append(
                    f"{dep.deploy_key}: no previous deploy commit - only its own commit checked."
                )
            if res.data.get("more"):
                partial.append(f"{dep.deploy_key}: more than {b.max_commits} commits, "
                               f"only the newest {b.max_commits} checked")  # fmt: skip
            for item in res.data.get("items", []):
                stats.commits_checked += 1
                for f in item.get("files", []):
                    lang = cp.language_of(f["path"])
                    if lang is None:
                        continue  # not source code (config, docs): nothing to parse
                    if f.get("truncated"):
                        stats.files_skipped += 1
                        partial.append(f"{f['path']} at {item['sha'][:7]} too long to check")
                        continue
                    try:
                        hits = cp.analyze_diff(lang, f["path"], f["content_after"], f["patch"])
                    except cp.PatchMismatchError:
                        stats.files_skipped += 1
                        partial.append(
                            f"{f['path']} at {item['sha'][:7]}: diff does not fit the file"
                        )
                        continue
                    stats.files_checked += 1
                    for hit in hits:
                        removed = hit.symbol if hit.rule == "field_removed" else None
                        excerpt = hunk_excerpt(f["patch"], hit.line, removed)
                        ev = _commit_evidence(item, excerpt, res.tool_call_id)
                        evidence.setdefault(ev.evidence_key, ev)
                        symbol = (f"{hit.container}.{hit.symbol}"
                                  if hit.rule == "field_removed" and hit.container else hit.symbol)  # fmt: skip
                        findings.append(CodeFinding(
                            rule=hit.rule,  # type: ignore[arg-type]
                            deploy_key=dep.deploy_key, deploy_started_at=dep.started_at,
                            service_key=dep.service_key, commit_sha=item["sha"],
                            committed_at=_dt(item["committed_at"]),
                            pull_request=item.get("pull_request"), path=f["path"], line=hit.line,
                            symbol=symbol[:200],
                            statement=(f"{dep.deploy_key} shipped commit {item['sha'][:7]}"
                                       + (f" (PR #{item['pull_request']})" if item.get("pull_request") else "")
                                       + f": {hit.detail}")[:500],
                            evidence_id=ev.evidence_key,
                        ))  # fmt: skip
        findings = findings[:30]
        for i, fnd in enumerate(findings):
            if fnd.rule != "field_removed":
                continue
            if time.monotonic() > deadline:
                partial.append(f"impact of {fnd.symbol} not looked up (task deadline)")
                continue
            res = await call(self.deps.tools, tracer, "find_references",
                             {"symbol": fnd.symbol, "limit": b.max_refs}, ctx)  # fmt: skip
            if isinstance(res, Failure):
                failures.append(f"find_references {fnd.symbol}: {res.reason}")
                continue
            items = res.data.get("items", [])
            other = [r for r in items if r.get("service_key") != fnd.service_key]
            # stored on THIS incident (visible per role): only code every engineer may read
            refs = [r for r in other if r.get("open_to_engineering")]
            stats.not_shared += len(other) - len(refs)  # trace only (agent output)
            services = sorted({r.get("service_key") or r["repository"] for r in refs})
            stats.impact_refs += len(refs)
            capped = len(items) >= b.max_refs
            if capped:
                partial.append(f"impact of {fnd.symbol}: more than {b.max_refs} references")
            kept = refs[:MAX_REFS_PER_FINDING]
            for r in kept:
                ev = _ref_evidence(r, fnd.symbol, res.tool_call_id)
                evidence.setdefault(ev.evidence_key, ev)
            impact = [ImpactRef(service_key=r.get("service_key"), repository=r["repository"],
                                path=r["path"], line=r["line"], name=r["name"],
                                evidence_id=r["evidence_id"]) for r in kept]  # fmt: skip
            scope = "in code every engineer may read"
            findings[i] = fnd.model_copy(update={
                "impact": impact,
                "statement": (fnd.statement + (
                    f" Still read by {len(services)}{'+' if capped else ''} other service(s) "
                    f"{scope}: {', '.join(services[:8])}." if services else
                    f" No other service {scope} uses the old name."))[:500],
            })  # fmt: skip
        stats.findings = len(findings)
        if failures and not stats.commits_checked:
            return AgentRunResult(status="FAILED", error="; ".join(failures), code=stats,
                                  trace=tracer.finish())  # fmt: skip
        # every id a fact or an impact cites is in the evidence that goes back (review: the
        # 100-item cap could drop CODE evidence that facts still cited)
        stored = dict(list(evidence.items())[:MAX_EVIDENCE])
        findings = [f.model_copy(update={"impact": [x for x in f.impact if x.evidence_id in stored]})
                    for f in findings if f.evidence_id in stored]  # fmt: skip
        facts = [
            Fact(
                statement=f.statement,
                produced_by=AGENT_NAME,
                evidence=[
                    EvidenceRef(evidence_id=f.evidence_id, kind=EvidenceKind.COMMIT,
                                source_system="devdata.commits"),
                    *[EvidenceRef(evidence_id=x.evidence_id, kind=EvidenceKind.CODE,
                                  source_system="rag.code_refs") for x in f.impact[:5]],
                ],
            )
            for f in findings
        ]  # fmt: skip
        if stats.not_shared:
            notes.append(f"{stats.not_shared} reference(s) in code not shared with all engineers "
                         "were not kept (they would be stored on this incident).")  # fmt: skip
        notes.append(f"{stats.commits_checked} commit(s) in {stats.deploys_checked} deploy(s), "
                     f"{stats.files_checked} file(s): {stats.findings} finding(s) by rule.")  # fmt: skip
        if not findings and stats.files_checked:
            notes.append("no rule matched in the files checked: no query/remote call added "
                         "inside a loop, no numeric limit changed, no field removed.")  # fmt: skip
        problems = failures + partial
        return AgentRunResult(
            status="SUCCEEDED", degraded="; ".join(problems)[:1000] or None, notes=notes[:30],
            facts=facts[:30], code_findings=findings, code=stats,
            evidence=list(stored.values()), trace=tracer.finish(),
        )  # fmt: skip


def _dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
