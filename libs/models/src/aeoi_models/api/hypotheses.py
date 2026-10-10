"""Phase 11 output types: observations, hypotheses, critique, validation (ADR-021).
No dependency on agents.py (AgentRunResult embeds these)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from aeoi_models.findings import ConfidenceBand, EvidenceRef

ObsRef = str  # "o1".."o99": short refs the model cites; code maps them to evidence ids
CauseKind = Literal["change", "saturation", "traffic", "alternative"]
HypothesisStatus = Literal["PROPOSED", "CHALLENGED", "VALIDATED", "REJECTED"]


class Observation(BaseModel):
    """One timestamped thing an evidence agent found, built by CODE from its typed output."""

    ref: ObsRef = Field(pattern=r"^o[0-9]{1,3}$")
    agent: str
    role: Literal["effect", "resource", "traffic", "change", "steady"]
    service_key: str
    subject: str = Field(max_length=120, description="metric, error code or deploy key")
    at: datetime
    statement: str = Field(max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=4)
    # effects only: a sustained metric shift can anchor "the first symptom"; a log cluster or a
    # transient blip cannot rule a deploy out on timing (review finding, ADR-021)
    sustained: bool = False


class HypothesisOut(BaseModel):
    key: str = Field(pattern=r"^h[0-9]{1,2}$")
    cause_kind: CauseKind
    origin: Literal["code", "critic"]
    statement: str = Field(max_length=600)
    rank: int = Field(ge=1)
    confidence: ConfidenceBand
    rubric: str = Field(max_length=300, description="why this band (code)")
    status: HypothesisStatus = "PROPOSED"
    supporting: list[ObsRef] = Field(min_length=1)
    contradicting: list[ObsRef] = Field(default_factory=list)
    supporting_evidence: list[EvidenceRef] = Field(min_length=1)
    contradicting_evidence: list[EvidenceRef] = Field(default_factory=list)
    explanation: str = Field(
        default="", max_length=600, description="LLM; cites `explanation_refs`"
    )
    explanation_refs: list[ObsRef] = Field(default_factory=list)
    ranked_by: Literal["llm", "rubric"] = "rubric"
    critic_verdict: Literal["supported", "weakened", "refuted", "not_reviewed"] = "not_reviewed"
    critic_missing: str = Field(default="", max_length=300)
    # refs the critic cited AGAINST this cause that cannot contradict this kind of cause (code
    # rule `candidates.can_contradict`): shown, never counted in the band (Mac run, ADR-021 D)
    critic_disputed: list[ObsRef] = Field(default_factory=list, max_length=8)
    # a critic "alternative" that names THIS deploy as its cause is a more specific mechanism
    # of this hypothesis, not a rival cause: model text, cites `refinement_refs`
    refinement: str = Field(default="", max_length=300)
    refinement_refs: list[ObsRef] = Field(default_factory=list, max_length=4)


class RuledOut(BaseModel):
    """A cause the evidence argues AGAINST (e.g. traffic stayed within baseline)."""

    cause_kind: CauseKind
    statement: str = Field(max_length=400)
    contradicting: list[ObsRef] = Field(min_length=1)
    contradicting_evidence: list[EvidenceRef] = Field(min_length=1)


class Critique(BaseModel):
    ran: bool = False
    reviews: int = 0
    contradictions_added: int = 0
    contradictions_ineligible: int = 0  # cited against a cause they cannot contradict
    citations_dropped: int = 0  # refs the critic cited that do not exist / are not allowed
    alternatives_proposed: int = 0
    alternatives_accepted: int = 0
    alternatives_merged: int = 0  # "alternatives" that refine a candidate deploy
    no_alternative_reason: str = Field(default="", max_length=400)
    error: str | None = None
    model: str | None = None  # who critiqued: must differ from the ranker's model


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = Field(default="", max_length=500)


class ValidationReport(BaseModel):
    passed: bool
    checks: list[ValidationCheck]
    dropped: list[str] = Field(default_factory=list, description="hypothesis keys removed")


# ------------------------------------------------------------------ incident-service contract
class HypothesisIn(BaseModel):
    """One validated hypothesis (or a ruled-out cause, status REJECTED) for incident-service."""

    key: str = Field(pattern=r"^(h|r)[0-9]{1,2}$")  # h = hypothesis, r = ruled out
    statement: str = Field(min_length=3, max_length=600)
    confidence: ConfidenceBand
    status: HypothesisStatus
    rank: int = Field(ge=1, le=100)
    produced_by: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    supports: list[str] = Field(default_factory=list, max_length=40)  # evidence keys
    contradicts: list[str] = Field(default_factory=list, max_length=40)
    detail: dict[str, str | int | list[str] | None] = Field(default_factory=dict)


class HypothesisBatch(BaseModel):
    """POST /v1/incidents/{id}/hypotheses (scope hypotheses:write). Idempotent per
    (investigation_id, key): a resent batch inserts nothing."""

    investigation_id: UUID
    items: list[HypothesisIn] = Field(min_length=1, max_length=30)


class HypothesisEdge(BaseModel):
    evidence_key: str
    kind: str
    stance: Literal["SUPPORTS", "CONTRADICTS"]


class HypothesisRecord(BaseModel):
    id: UUID
    investigation_id: UUID | None
    key: str | None
    statement: str
    confidence: str
    status: str
    rank: int
    produced_by: str
    detail: dict[str, object]
    edges: list[HypothesisEdge]
    hidden_edges: int = Field(description="evidence edges of kinds this caller may not read")
    created_at: datetime


class HypothesisCreatedPayload(BaseModel):
    """Event HypothesisCreated (topic incident.lifecycle, via the outbox)."""

    incident_id: UUID
    investigation_id: UUID
    keys: list[str] = Field(max_length=30)
    challenged: list[str] = Field(max_length=30)


def observations_fingerprint(obs: list[Observation]) -> str:
    """Ranker and critic must reason over the SAME observations - not only the same refs
    (review finding: the critic checked only that its refs existed)."""
    import hashlib
    import json

    raw = json.dumps([o.model_dump(mode="json") for o in obs], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()
