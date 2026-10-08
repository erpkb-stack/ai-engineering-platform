"""Authorization policy (code, not an LLM): who may call which tool, for whom.

Effective permission = USER permissions ∩ AGENT allow-list (architecture.md §14). An agent
acting for an Engineer can never do more than that Engineer could, and never more than the
agent's own narrow tool list.

Order of checks (first failure wins, every outcome is recorded):
  1 user has every permission the tool requires
  2 agent (if any) is known and the tool is in its allow-list
  3 side effect: CONSEQUENTIAL needs an approval_id AND a verified approval (Phase 16).
    Until approvals exist, verification is impossible -> DENY (fail closed).
The agent NAME itself is verified earlier (api.py): only services listed in
`trusted_services` may assert an agent name, and only the names listed for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aeoi_tools.contracts import SideEffect, ToolContext, ToolSpec


class DenyReason(StrEnum):
    """Stable codes: dashboards and tests key on these, so never rename one."""

    UNKNOWN_TOOL = "unknown_tool"
    MISSING_PERMISSION = "missing_permission"
    UNKNOWN_AGENT = "unknown_agent"
    NOT_IN_AGENT_ALLOWLIST = "not_in_agent_allowlist"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_UNVERIFIABLE = "approval_unverifiable"
    RATE_LIMITED = "rate_limited"
    SERVICE_WITHOUT_USER = "service_without_user"
    UNTRUSTED_AGENT_ASSERTION = "untrusted_agent_assertion"
    INVALID_ON_BEHALF_OF = "invalid_on_behalf_of"
    DELEGATION_SCOPE_MISMATCH = "delegation_scope_mismatch"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: DenyReason | None = None
    detail: str = ""

    @classmethod
    def allow(cls) -> Decision:
        return cls(True)

    @classmethod
    def deny(cls, reason: DenyReason, detail: str = "") -> Decision:
        return cls(False, reason, detail)


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tools: frozenset[str] = Field(max_length=50)
    description: str = Field(default="", max_length=300)


class PolicyConfig(BaseModel):
    """config/agents.yaml - validated at boot against the registry (unknown tool = crash)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = 1
    # service name (token sub without "service:") -> agent names it may act as
    trusted_services: dict[str, frozenset[str]] = Field(default_factory=dict)
    agents: dict[str, AgentPolicy] = Field(default_factory=dict)

    @field_validator("agents")
    @classmethod
    def _names(cls, v: dict[str, AgentPolicy]) -> dict[str, AgentPolicy]:
        for name in v:
            if not name.replace("_", "").isalnum() or len(name) > 64:
                raise ValueError(f"bad agent name: {name!r}")
        return v

    @classmethod
    def load(cls, path: Path, tool_names: set[str]) -> PolicyConfig:
        cfg = cls.model_validate(yaml.safe_load(path.read_text()))
        cfg.check(tool_names)
        return cfg

    def check(self, tool_names: set[str]) -> None:
        for agent, pol in self.agents.items():
            unknown = pol.tools - tool_names
            if unknown:
                raise ValueError(
                    f"agents.yaml: agent {agent} lists unknown tools {sorted(unknown)}"
                )
        for svc, agents in self.trusted_services.items():
            unknown = agents - set(self.agents)
            if unknown:
                raise ValueError(f"agents.yaml: service {svc} trusts unknown agents {unknown}")

    def may_assert(self, service: str, agent: str) -> bool:
        return agent in self.trusted_services.get(service, frozenset())


def decide(spec: ToolSpec, ctx: ToolContext, cfg: PolicyConfig) -> Decision:
    missing = sorted(p.value for p in spec.permissions - ctx.user.permissions)
    if missing:
        return Decision.deny(DenyReason.MISSING_PERMISSION, ",".join(missing))
    if ctx.agent is not None:
        agent = cfg.agents.get(ctx.agent)
        if agent is None:
            return Decision.deny(DenyReason.UNKNOWN_AGENT, ctx.agent)
        if spec.name not in agent.tools:
            return Decision.deny(DenyReason.NOT_IN_AGENT_ALLOWLIST, ctx.agent)
    if spec.side_effect is SideEffect.CONSEQUENTIAL:
        if ctx.approval_id is None:
            return Decision.deny(DenyReason.APPROVAL_REQUIRED)
        # Phase 16 replaces this with: approval exists, APPROVED, not expired, not used,
        # for THIS action + arguments, approved by someone other than the requester.
        return Decision.deny(DenyReason.APPROVAL_UNVERIFIABLE, "approval service not available")
    return Decision.allow()
