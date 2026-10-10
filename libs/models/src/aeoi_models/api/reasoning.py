"""Phase 11 contract: hypotheses, critique, validation (ADR-021).

Who writes what (owner decision): CODE proposes candidate causes from typed evidence and sets
the confidence band by a rubric; the LLM only RANKS and EXPLAINS (hypothesis agent) and
ATTACKS (critic agent), every claim citing observation refs that code maps back to evidence.
Deterministic validation (orchestrator) checks the result before anything is stored.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aeoi_models.api.agents import AgentRunResult, LlmRoute
from aeoi_models.api.hypotheses import (
    CauseKind,
    Critique,
    HypothesisOut,
    HypothesisStatus,
    Observation,
    ObsRef,
    RuledOut,
    ValidationCheck,
    ValidationReport,
)

__all__ = [
    "CauseKind",
    "Critique",
    "HypothesisOut",
    "HypothesisStatus",
    "ObsRef",
    "Observation",
    "ReasoningTask",
    "RuledOut",
    "ValidationCheck",
    "ValidationReport",
]


class ReasoningTask(BaseModel):
    """Input of the hypothesis and critic agents: the evidence agents' RESULTS (not prose)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    investigation_id: UUID
    task_id: UUID | None = None
    incident_title: str | None = Field(default=None, max_length=300)
    detected_at: datetime | None = None
    results: dict[str, AgentRunResult] = Field(max_length=8)
    # critic only: what to attack. Statements + refs written by CODE, never the ranker's prose
    hypotheses: list[HypothesisOut] = Field(default_factory=list, max_length=12)
    # critic only: sha of the observations the ranker used; the critic refuses on a mismatch
    observations_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    llm_route: LlmRoute | None = None
    llm_cache: bool = True
