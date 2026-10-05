"""Evidence-backed findings: the core invariant of AEOI.

Facts, hypotheses and recommendations are DIFFERENT types, so they can never be
mixed in one field (AI safety principle #10). The schema enforces the rules that
a prompt can only ask for:
- a Fact must cite at least one piece of evidence;
- a Hypothesis must cite supporting evidence and lists contradicting evidence;
- HIGH confidence is impossible while contradicting evidence is unresolved
  and with fewer than 2 independent pieces of support;
- a Recommendation that changes production requires human approval.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

EvidenceId = Annotated[str, StringConstraints(pattern=r"^[A-Z]+-[A-Za-z0-9._-]+$", max_length=80)]


class EvidenceKind(StrEnum):
    LOG = "LOG"
    METRIC = "METRIC"
    TRACE = "TRACE"
    DEPLOY = "DEPLOY"
    CONFIG = "CONFIG"
    COMMIT = "COMMIT"
    PR = "PR"
    DOC = "DOC"
    RUNBOOK = "RUNBOOK"
    INCIDENT = "INCIDENT"
    ALERT = "ALERT"


class ConfidenceBand(StrEnum):
    """Rubric band, not an LLM-invented percentage (architecture.md C5)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: EvidenceId
    kind: EvidenceKind
    source_system: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _prefix_matches_kind(self) -> EvidenceRef:
        if not self.evidence_id.startswith(f"{self.kind.value}-"):
            raise ValueError(f"evidence_id {self.evidence_id!r} must start with '{self.kind}-'")
        return self


class _FindingBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=3, max_length=2000)
    produced_by: str = Field(min_length=1, max_length=64)  # agent name


class Fact(_FindingBase):
    """Something observed directly in evidence. No interpretation."""

    type: Literal["fact"] = "fact"
    evidence: list[EvidenceRef] = Field(min_length=1)


class Hypothesis(_FindingBase):
    """A possible explanation. Always carries the evidence for AND against it."""

    type: Literal["hypothesis"] = "hypothesis"
    supporting: list[EvidenceRef] = Field(min_length=1)
    contradicting: list[EvidenceRef] = Field(default_factory=list)
    confidence: ConfidenceBand
    alternatives: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _high_confidence_needs_strong_evidence(self) -> Hypothesis:
        if self.confidence is ConfidenceBand.HIGH:
            if self.contradicting:
                raise ValueError("HIGH confidence is not allowed with contradicting evidence")
            if len({e.evidence_id for e in self.supporting}) < 2:
                raise ValueError("HIGH confidence needs at least 2 distinct supporting evidence")
        return self


class Recommendation(_FindingBase):
    """A proposed next step. Consequential actions need a recorded human approval."""

    type: Literal["recommendation"] = "recommendation"
    action: str = Field(min_length=3, max_length=500)
    based_on: list[EvidenceRef] = Field(min_length=1)
    consequential: bool
    requires_approval: bool

    @model_validator(mode="after")
    def _consequential_requires_approval(self) -> Recommendation:
        if self.consequential and not self.requires_approval:
            raise ValueError("a consequential recommendation must require human approval")
        return self


Finding = Annotated[Fact | Hypothesis | Recommendation, Field(discriminator="type")]
