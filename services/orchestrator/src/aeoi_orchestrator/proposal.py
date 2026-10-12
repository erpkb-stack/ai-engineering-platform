"""Phase 16 (ADR-026): when an investigation PROPOSES an action. CODE ONLY - no model decides.

Rule v1: the top validated hypothesis is a deploy ("change") that is still a successful deploy
with an earlier release to go back to -> propose rollback_deployment of exactly that deploy.
Anything else -> no proposal, with the reason (shown in the investigation summary).

A proposal is only a REQUEST: an incident commander who did not start the investigation must
approve it, and the gateway re-checks the target when it runs (a newer deploy -> refused).

The recommendation is a snapshot for the approver, written by code from recorded results:
- every incident reader sees the hypothesis, band, deploy and config keys;
- `way_back` quotes code (paths, symbols): incident-service shows it only to code:read readers.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from aeoi_models.api.agents import AgentRunResult, DeployChange
from aeoi_models.api.approvals import ActionRequest, RollbackArgs
from aeoi_models.api.hypotheses import HypothesisOut, Observation

RULE = "v1: top hypothesis is a VALIDATED deploy"
DEPLOY_KEY = re.compile(r"^DEPLOY-[0-9]{1,24}$")
MAX_CODE_LINES = 5


def _flag_like(key: str) -> bool:
    """Name heuristic, same as the release rubric ([P]: a flag registry in Production)."""
    k = key.lower()
    return "flag" in k or "feature" in k


def _changed(d: DeployChange) -> list[str]:
    return sorted({c.key for c in d.config_changes if c.before != c.after})


def way_back(d: DeployChange, code: AgentRunResult | None) -> list[str]:
    prev = (d.previous_commit_sha or "")[:12]
    lines = [
        f"Rollback = a NEW deploy of the previous successful release of {d.service_key} "
        f"(commit {prev}). The tool writes it only if {d.deploy_key} is still the running "
        "deploy of that service: a newer deploy makes it refuse."
    ]
    keys = _changed(d)
    if keys:
        lines.append(f"It also sets back the config {d.deploy_key} changed: {', '.join(keys)}.")
    flags = [k for k in keys if _flag_like(k)]
    if flags:
        lines.append(
            f"Flag(s) {', '.join(flags)} changed with this deploy. Switching a flag back alone "
            "does not undo code that does not read it; the rollback undoes both."
        )
    findings = [f for f in (code.code_findings if code else []) if f.deploy_key == d.deploy_key]
    for f in findings[:MAX_CODE_LINES]:
        lines.append(f"Shipped code ({f.rule}): {f.path}:{f.line} {f.symbol}"[:300])
    if len(findings) > MAX_CODE_LINES:
        lines.append(f"... and {len(findings) - MAX_CODE_LINES} more code finding(s).")
    return lines


def propose(
    investigation_id: UUID,
    kept: list[HypothesisOut],
    observations: list[Observation],
    deployment: AgentRunResult | None,
    code: AgentRunResult | None,
) -> tuple[ActionRequest | None, str]:
    """(request, why). `request` is None when the rule does not hold; `why` says which part."""
    if not kept:
        return None, "no hypothesis survived validation"
    top = kept[0]
    if top.cause_kind != "change":
        return None, f"top cause is {top.cause_kind}, not a deploy"
    if top.status != "VALIDATED":
        return None, f"top deploy hypothesis is {top.status}, not VALIDATED"
    by_ref = {o.ref: o for o in observations}
    anchor = by_ref.get(top.supporting[0])
    if anchor is None or anchor.role != "change" or not DEPLOY_KEY.match(anchor.subject):
        return None, "top hypothesis does not name a deploy"
    key = anchor.subject
    d = next((c for c in (deployment.changes if deployment else []) if c.deploy_key == key), None)
    if d is None:
        return None, f"{key} is not in the deployment agent's result"
    if d.status != "SUCCEEDED":
        return None, f"{key} is {d.status}: nothing running to roll back"
    if not d.previous_commit_sha:
        return None, f"no earlier successful deploy of {d.service_key} is known"
    reason = (f"Investigation {investigation_id}: {top.statement}")[:500]
    rec: dict[str, Any] = {
        "rule": RULE,
        "hypothesis_key": top.key,
        "statement": top.statement,
        "confidence": str(top.confidence),
        "rubric": top.rubric,
        "deploy_key": key,
        "service_key": d.service_key,
        "version": d.version,
        "config_restored": _changed(d),
        "way_back": way_back(d, code),
    }
    return ActionRequest(
        action="rollback_deployment",
        args=RollbackArgs(deploy_key=key, reason=reason),
        recommendation=rec,
        evidence_keys=[e.evidence_id for e in top.supporting_evidence][:50],
        investigation_id=investigation_id,
    ), f"proposed rollback of {key}"
