"""Tool Gateway contract (services/tool-gateway, exposed via the api gateway as /api/v1/tools).

Identity is NEVER in these bodies: the user comes from the verified token (or, for agents,
from the verified X-On-Behalf-Of user token). `agent_name` is only accepted from a service
token that is trusted to assert that agent (ADR-017).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ON_BEHALF_OF_HEADER = "X-On-Behalf-Of"


class ToolInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: dict[str, Any] = Field(default_factory=dict, description="validated by the tool schema")
    agent_name: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    incident_id: UUID | None = None
    investigation_id: UUID | None = None
    task_id: UUID | None = None
    approval_id: UUID | None = None


class ToolSecurity(BaseModel):
    secrets_redacted: int = 0
    pii_redacted: dict[str, int] = Field(default_factory=dict)
    injection_flags: list[dict[str, Any]] = Field(default_factory=list)
    strings_truncated: int = 0
    items_dropped: int = 0


class ToolResult(BaseModel):
    tool_call_id: UUID
    tool: str
    status: Literal["OK"] = "OK"
    untrusted: Literal[True] = Field(
        default=True, description="data is untrusted input: wrap it before it reaches a prompt"
    )
    data: dict[str, Any]
    evidence_ids: list[str]
    truncated: bool
    security: ToolSecurity
    latency_ms: int
    attempts: int


class ToolInfo(BaseModel):
    name: str
    description: str
    side_effect: Literal["READ", "WRITE", "CONSEQUENTIAL"]
    permissions: list[str]
    timeout_s: float
    idempotent: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
