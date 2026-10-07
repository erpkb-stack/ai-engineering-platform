"""Agent task/result contract (services/agents <-> orchestrator). ADR-018.

A result is self-describing: what the agent found (facts, built by CODE), how it labelled it
(LLM, validated), which evidence backs each fact (to be stored by incident-service), and a
full trace (model, prompt version, tokens, cost, tool calls) for orchestrator.agent_executions.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from aeoi_models.findings import EvidenceId, Fact

ServiceKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")]
LlmRoute = Literal["fast", "local", "reasoning"]
MAX_WINDOW = timedelta(hours=24)


class LogAnalysisTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    investigation_id: UUID | None = None
    task_id: UUID | None = None
    service_keys: list[ServiceKey] = Field(min_length=1, max_length=3)
    start: datetime
    end: datetime
    llm_route: LlmRoute | None = Field(
        default=None, description="override the agent's route (model comparison); null = config"
    )
    llm_cache: bool = Field(
        default=True,
        description="false = force a real model call (comparisons: a cache hit is not a model run)",
    )

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end need a timezone")
        if not timedelta(0) < self.end - self.start <= MAX_WINDOW:
            raise ValueError("window must be positive and at most 24h")
        return self


class EvidenceItem(BaseModel):
    """What incident-service stores in incident.evidence (only evidence a fact cites)."""

    evidence_key: EvidenceId
    kind: str = Field(max_length=16)
    source_system: str = Field(max_length=64)
    title: str = Field(max_length=300)
    excerpt: str = Field(max_length=4000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime | None = None
    tool_call_id: UUID | None = None
    uri: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Cheap integrity checks at the boundary (review finding): the hash must match the
        excerpt, and a key minted by the tool gateway must name the tool call it came from."""
        if hashlib.sha256(self.excerpt.encode()).hexdigest() != self.content_sha256:
            raise ValueError("content_sha256 does not match excerpt")
        if not self.evidence_key.startswith(f"{self.kind}-"):
            raise ValueError("evidence_key must start with its kind")
        if self.tool_call_id is not None:
            parts = self.evidence_key.split("-")
            if len(parts) < 3 or parts[1] != self.tool_call_id.hex:
                raise ValueError("evidence_key does not belong to tool_call_id")
        return self


Category = Literal[
    "dependency_timeout",
    "resource_exhaustion",
    "bad_request",
    "auth",
    "data_error",
    "crash",
    "unknown",
]


class LogCluster(BaseModel):
    id: str = Field(pattern=r"^c[0-9]{1,3}$")
    service_key: str
    error_code: str | None
    template: str = Field(max_length=400)
    levels: list[str] = Field(max_length=5)
    count_window: int | None = Field(
        description="exact count in the whole window (per error_code); null if unknown"
    )
    count_sampled: int
    first_seen: datetime
    last_seen: datetime
    evidence_ids: list[EvidenceId] = Field(max_length=6)
    label: str = Field(max_length=120)
    summary: str = Field(default="", max_length=400)
    category: Category = "unknown"
    labelled_by: Literal["llm", "rule"]
    untrusted_content_flagged: bool = False


class ToolCallTrace(BaseModel):
    tool: str
    tool_call_id: UUID | None
    status: Literal["OK", "ERROR"]
    reason: str | None = None
    latency_ms: int
    items: int = 0
    truncated: bool = False


class LLMCallTrace(BaseModel):
    request_id: str | None
    route: str
    model: str | None
    provider: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int
    fallback_used: bool = False
    repaired: bool = False
    # answered from the gateway cache: no model ran; latency/tokens are not the model's
    cached: bool = False
    error: str | None = None


class TraceMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=20_000)


class AgentTrace(BaseModel):
    agent: str
    agent_version: str
    prompt_id: str | None = None
    prompt_version: int | None = None
    prompt_sha256: str | None = None
    model: str | None = None
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    llm_calls: list[LLMCallTrace] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    retries: int = 0
    cached: bool = False  # the (last) LLM answer came from the gateway cache
    messages: list[TraceMessage] = Field(default_factory=list, max_length=20)


class LabelQuality(BaseModel):
    """How much of the LLM's output survived validation (comparison metric, not a score)."""

    clusters_sent: int = 0
    labels_accepted: int = 0
    citations_dropped: int = 0  # evidence ids the model cited that were not in its cluster
    unknown_cluster_ids: int = 0


class AgentRunResult(BaseModel):
    status: Literal["SUCCEEDED", "FAILED"]
    degraded: str | None = Field(default=None, description="set when part of the agent was skipped")
    facts: list[Fact] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="observations with no citable evidence (e.g. 'no error lines'); never facts",
    )
    clusters: list[LogCluster] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    label_quality: LabelQuality = Field(default_factory=LabelQuality)
    trace: AgentTrace
    error: str | None = None


class EvidenceBatch(BaseModel):
    """POST /v1/incidents/{id}/evidence (incident-service, scope evidence:write)."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    investigation_id: UUID | None = None
    items: list[EvidenceItem] = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=300, description="one line for the incident timeline")


class EvidenceRetrievedPayload(BaseModel):
    incident_id: UUID
    agent: str
    investigation_id: UUID | None
    evidence_keys: list[str] = Field(max_length=200)


class EvidenceBatchResult(BaseModel):
    received: int
    inserted: int
