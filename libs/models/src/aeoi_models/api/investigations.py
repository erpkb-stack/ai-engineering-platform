"""Orchestrator API (Phase 9, ADR-019): start, read, trace, cancel investigations."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aeoi_models.api.agents import MAX_WINDOW, AgentName, LlmRoute


class StartInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident: str = Field(min_length=1, max_length=64, description="INC-10001 or the uuid")
    # dev/eval knobs (model comparison); production callers omit them
    llm_route: LlmRoute | None = None
    llm_cache: bool = True
    start: datetime | None = None
    end: datetime | None = None
    # Phase 10: run only these agents (must be a subset of the orchestrator's configured set).
    # Model comparison runs only log_analysis: the other agents have no model to compare.
    agents: list[AgentName] | None = Field(default=None, min_length=1, max_length=4)
    # Phase 11: hypotheses + critic after the evidence agents (null = orchestrator config).
    # The Phase 8 model comparison turns it off: it compares the log agent only.
    reasoning: bool | None = None

    @model_validator(mode="after")
    def _window(self) -> StartInvestigation:
        if self.agents is not None and len(set(self.agents)) != len(self.agents):
            raise ValueError("agents must not repeat")
        if (self.start is None) != (self.end is None):
            raise ValueError("give both start and end, or neither")
        for v in (self.start, self.end):
            if v is not None and v.tzinfo is None:
                raise ValueError("start/end need a timezone")
        if self.start and self.end and not timedelta(0) < self.end - self.start <= MAX_WINDOW:
            raise ValueError("window must be positive and at most 24h")
        return self


class TaskOut(BaseModel):
    task_id: UUID
    agent: str
    status: str
    attempt: int
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    model: str | None = None
    latency_ms: int | None = None
    cost_usd: Decimal | None = None
    degraded: str | None = None


class InvestigationOut(BaseModel):
    investigation_id: UUID
    incident_id: UUID
    status: str
    requested_by: str
    started_at: datetime
    finished_at: datetime | None
    deadline_at: datetime | None
    spent_usd: Decimal
    error: str | None
    plan: dict[str, Any]
    tasks: list[TaskOut] = Field(default_factory=list)
    evidence_inserted: int | None = None
    # where the graph is: next node(s) from the last checkpoint ([] = finished or not started)
    graph_next: list[str] = Field(default_factory=list)


class TraceMessageOut(BaseModel):
    seq: int
    role: str
    content: str


class ExecutionTraceOut(BaseModel):
    execution_id: UUID
    task_id: UUID
    agent: str
    agent_version: str
    status: str
    model: str | None
    prompt_id: str | None
    prompt_version: int | None
    input_tokens: int
    output_tokens: int
    latency_ms: int | None
    cost_usd: Decimal
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    output: dict[str, Any]
    messages: list[TraceMessageOut] = Field(default_factory=list)
    # true = caller may read the incident but not what the agent read (e.g. logs:read):
    # metadata only, no prompts / clusters / facts text (review finding)
    redacted: bool = False


class InvestigationTrace(BaseModel):
    investigation_id: UUID
    executions: list[ExecutionTraceOut]


class CancelInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=200)
