"""Deployment agent (architecture.md §10 #6). Code only - no LLM call (ADR-020).

1 get_deployment per incident service, from `lookback_h` before the window to its end
2 get_config_diff for every deploy that touched config (values of secret keys arrive
  already redacted by the gateway: "a secret changed" is a fact, its value is not)
3 one Fact per deploy: what, when, status, commit, changed keys, and minutes before
  detection - TIME ARITHMETIC, not a causal claim. "This deploy caused it" is a hypothesis
  for Phase 11, where the critic must also see the deploys that changed nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aeoi_agents.common import Failure, call, context, hhmm, ts
from aeoi_agents.config import DeploymentBudget
from aeoi_agents.trace import Tracer
from aeoi_models.api.agents import (
    AgentRunResult,
    AgentTask,
    ConfigChangeOut,
    DeployChange,
    EvidenceItem,
)
from aeoi_models.findings import EvidenceKind, EvidenceRef, Fact
from aeoi_tool_client import ToolClient

AGENT_NAME = "deployment"
AGENT_VERSION = "1.0.0"
SOURCE = "devdata.deployments"


@dataclass
class Deps:
    tools: ToolClient
    budget: DeploymentBudget


def _val(v: Any) -> str:
    return json.dumps(v, default=str)[:80]


class DeploymentAgent:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def run(
        self, task: AgentTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        b = self.deps.budget
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        ctx = context(task, AGENT_NAME, user_token, correlation_id)
        deadline = time.monotonic() + b.task_deadline_s
        # the gateway caps one query at 24 h: shorten the look-back for long windows (review)
        since = task.start - min(
            timedelta(hours=b.lookback_h), timedelta(hours=24) - (task.end - task.start)
        )
        facts: list[Fact] = []
        notes: list[str] = []
        changes: list[DeployChange] = []
        evidence: list[EvidenceItem] = []
        failed: list[str] = []
        denied: Failure | None = None
        diffs_left = b.max_config_diffs
        answered = 0
        for svc in task.service_keys:
            if denied is not None or time.monotonic() > deadline:
                failed.append(f"{svc} (not checked: denied or deadline)")
                continue
            res = await call(
                self.deps.tools,
                tracer,
                "get_deployment",
                {
                    "service_key": svc,
                    "start": since.isoformat(),
                    "end": task.end.isoformat(),
                    "limit": b.max_deploys_per_service,
                },
                ctx,
            )
            if isinstance(res, Failure):
                if res.denied:
                    denied = res
                failed.append(f"{svc} ({res.reason})")
                continue
            answered += 1
            items = sorted(res.data.get("items", []), key=lambda d: str(d["started_at"]))
            if not items:
                notes.append(
                    f"{svc}: no production deploys from {hhmm(since)} to {hhmm(task.end)}."
                )
                continue
            if len(items) >= b.max_deploys_per_service:
                notes.append(f"{svc}: {len(items)} deploys shown; there may be more (limit).")
            for d in items:
                dep_ev = self._evidence("DEPLOY", d, res.tool_call_id, _deploy_excerpt(d))
                evidence.append(dep_ev)
                ev_ids = [dep_ev.evidence_key]
                config: list[ConfigChangeOut] = []
                if d.get("config_changed_keys"):
                    if diffs_left <= 0 or time.monotonic() > deadline:
                        notes.append(f"{d['deploy_key']}: config diff not fetched (budget).")
                    else:
                        diffs_left -= 1
                        cres = await call(
                            self.deps.tools,
                            tracer,
                            "get_config_diff",
                            {"deploy_key": d["deploy_key"]},
                            ctx,
                        )
                        if isinstance(cres, Failure):
                            notes.append(f"{d['deploy_key']}: config diff failed ({cres.reason}).")
                        elif cres.data.get("items"):
                            ci = cres.data["items"][0]
                            config = [ConfigChangeOut(**c) for c in ci.get("changes", [])]
                            cfg_ev = self._evidence(
                                "CONFIG", {**ci, "started_at": d["started_at"]},
                                cres.tool_call_id, json.dumps(ci.get("changes", []), default=str),
                            )  # fmt: skip
                            evidence.append(cfg_ev)
                            ev_ids.append(cfg_ev.evidence_key)
                change = self._change(task, d, config, ev_ids)
                changes.append(change)
                facts.append(self._fact(change, d))
        if answered == 0:
            why = denied.reason if denied else "every get_deployment call failed"
            return AgentRunResult(
                status="FAILED", error=f"no deploy data: {why}", trace=tracer.finish()
            )
        return AgentRunResult(
            status="SUCCEEDED",
            degraded=("no data: " + ", ".join(failed)) if failed else None,
            facts=facts,
            notes=notes[:30],
            changes=changes[:30],
            evidence=evidence,
            trace=tracer.finish(),
        )

    @staticmethod
    def _change(
        task: AgentTask, d: dict[str, Any], config: list[ConfigChangeOut], ev_ids: list[str]
    ) -> DeployChange:
        started = ts(d["started_at"])
        finished = ts(d["finished_at"]) if d.get("finished_at") else None
        before = None
        if task.detected_at is not None:
            # from the START (review finding): a rollout that began before detection and
            # finished after it is "in progress at detection", never "AFTER"
            before = round((task.detected_at - started).total_seconds() / 60, 1)
        return DeployChange(
            deploy_key=d["deploy_key"],
            service_key=d["service_key"],
            version=d["version"],
            status=d["status"],
            started_at=started,
            finished_at=finished,
            commit_sha=d["commit_sha"],
            deployed_by=d["deployed_by"],
            config_changes=config,
            minutes_before_detection=before,
            evidence_ids=ev_ids,
        )

    @staticmethod
    def _fact(c: DeployChange, d: dict[str, Any]) -> Fact:
        when = f"started {hhmm(c.started_at)}" + (
            f", finished {hhmm(c.finished_at)}" if c.finished_at else ", not finished"
        )
        if c.config_changes:
            cfg = "; config changed: " + ", ".join(
                f"{x.key} {_val(x.before)} -> {_val(x.after)}" for x in c.config_changes[:8]
            )
        elif d.get("config_changed_keys"):
            cfg = "; config keys changed: " + ", ".join(d["config_changed_keys"][:8])
        else:
            cfg = "; no config change"
        rel = ""
        if c.minutes_before_detection is not None:
            m = c.minutes_before_detection
            done = c.finished_at
            if m < 0:
                rel = f"; started {-m:g} min AFTER the incident was detected"
            elif done is None or (c.started_at + timedelta(minutes=m)) < done:
                rel = f"; started {m:g} min before detection and still rolling out at detection"
            else:
                after_done = round(m - (done - c.started_at).total_seconds() / 60, 1)
                rel = (
                    f"; started {m:g} min before the incident was detected "
                    f"(finished {after_done:g} min before)"
                )
        refs = [
            EvidenceRef(
                evidence_id=e,
                kind=EvidenceKind.CONFIG if e.startswith("CONFIG-") else EvidenceKind.DEPLOY,
                source_system=SOURCE,
            )
            for e in c.evidence_ids
        ]
        return Fact(
            statement=(
                f"{c.service_key}: deploy {c.deploy_key} version {c.version} ({c.status}), "
                f"{when}, commit {c.commit_sha[:12]} by {c.deployed_by}{cfg}{rel}."
            )[:2000],
            produced_by=AGENT_NAME,
            evidence=refs,
        )

    @staticmethod
    def _evidence(kind: str, item: dict[str, Any], tool_call_id: Any, text: str) -> EvidenceItem:
        excerpt = text[:4000]
        return EvidenceItem(
            evidence_key=str(item["evidence_id"]),
            kind=kind,
            source_system=SOURCE,
            title=f"{kind} {item['deploy_key']} {item['service_key']} {item['version']}"[:300],
            excerpt=excerpt,
            content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            observed_at=ts(item["started_at"]),
            tool_call_id=tool_call_id,
        )


def _deploy_excerpt(d: dict[str, Any]) -> str:
    keep = ("deploy_key", "service_key", "version", "environment", "status", "commit_sha",
            "deployed_by", "started_at", "finished_at", "config_changed_keys")  # fmt: skip
    return json.dumps({k: d.get(k) for k in keep}, default=str, separators=(",", ":"))
