"""Phase 16 (ADR-026): human approval of consequential actions - the contract shared by
incident-service (system of record), orchestrator (decide + execute), agents (executor) and
the tool gateway (verify + execute).

An approval is bound to ONE action with ONE exact set of arguments (`args_sha256`), ONE
incident and, once decided, ONE approver: the gateway executes only for that approver, only
those arguments, only once, only before it expires.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ActionName = Literal["rollback_deployment"]
Decision = Literal["APPROVED", "REJECTED", "MORE_INFO"]
ApprovalStatus = Literal[
    "PENDING", "APPROVED", "REJECTED", "MORE_INFO", "EXPIRED", "EXECUTED", "FAILED"
]
APPROVAL_TTL_MINUTES = 30


def args_sha256(action: str, args: dict[str, Any]) -> str:
    """THE canonical hash of an action + its arguments. Used when the approval is requested
    (incident-service) and when it is consumed (tool gateway, over the VALIDATED tool input):
    a single changed character in any argument is a different action."""
    canonical = json.dumps({"action": action, "args": args}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)  # fmt: skip
    return hashlib.sha256(canonical.encode()).hexdigest()


class RollbackArgs(BaseModel):
    """= tool-gateway RollbackIn (the gateway validates the same shape before hashing)."""

    model_config = ConfigDict(extra="forbid")

    deploy_key: str = Field(pattern=r"^DEPLOY-[0-9]{1,24}$")
    reason: str = Field(min_length=10, max_length=500)


class ActionRequest(BaseModel):
    """POST /v1/incidents/{ref}/actions (an SRE, by hand) or .../actions/proposals (the
    orchestrator, for the user who started the investigation)."""

    model_config = ConfigDict(extra="forbid")

    action: ActionName
    args: RollbackArgs
    recommendation: dict[str, Any] = Field(default_factory=dict, description="snapshot shown "
                                           "to the approver; code-written for AI proposals")  # fmt: skip
    evidence_keys: list[str] = Field(default_factory=list, max_length=50)
    investigation_id: UUID | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Decision
    reason: str = Field(min_length=10, max_length=1000, description="mandatory (ADR-007)")


class ConsumeIn(BaseModel):
    """Tool gateway -> incident-service, AS the acting user: use this approval for exactly
    this action, these arguments, this incident. Single use."""

    model_config = ConfigDict(extra="forbid")

    action: ActionName
    args_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    incident_id: UUID


class ConsumeOut(BaseModel):
    approval_id: UUID
    consumed_at: datetime
    resumed: bool = False  # True: already consumed by THIS approver for THESE args (a retry)


class OutcomeIn(BaseModel):
    """Orchestrator -> incident-service: what the execution did (recorded, never inferred)."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["EXECUTED", "FAILED"]
    tool_call_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ApprovalOut(BaseModel):
    id: UUID
    incident_id: UUID
    incident_key: str
    action: ActionName
    action_args: dict[str, Any]
    args_sha256: str
    recommendation: dict[str, Any]
    evidence_keys: list[str]
    source: Literal["investigation", "manual"]
    investigation_id: UUID | None
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    status: ApprovalStatus
    decided_by: UUID | None = None
    decider_role: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None
    consumed_at: datetime | None = None
    outcome: str | None = None
    outcome_detail: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: UUID | None = None


class ActionTask(BaseModel):
    """POST /v1/agents/action_executor/run (orchestrator -> agents), AS THE APPROVER."""

    model_config = ConfigDict(extra="forbid")

    approval_id: UUID
    incident_id: UUID
    action: ActionName
    args: dict[str, Any]


class ActionExecution(BaseModel):
    """What the action_executor agent did with ONE approved action (AgentRunResult.action).
    EXECUTED = the gateway ran the tool and recorded it; FAILED = refused or failed, with the
    gateway's stable reason (approval_unusable, approval_invalid, 409 stale target, ...)."""

    approval_id: UUID
    action: ActionName
    status: Literal["EXECUTED", "FAILED"]
    tool_call_id: UUID | None = None
    http_status: int | None = None
    reason: str | None = None
    detail: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class DecisionResult(BaseModel):
    """POST /api/v1/approvals/{id}/decision: the decision, and - when APPROVED - what the
    controlled execution did (EXECUTED / FAILED with the reason)."""

    approval: ApprovalOut
    executed: bool = False
    execution_error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class ApprovalEventPayload(BaseModel):
    """Events HumanReviewRequired / HumanApproved / HumanRejected / HumanMoreInfoRequested /
    ActionExecuted / ActionExpired (topic incident.lifecycle, via the outbox)."""

    approval_id: UUID
    incident_id: UUID
    key: str
    action: str
    status: str
    actor: str
