"""Phase 15 (ADR-025): release risk analysis - ADVISORY, never blocks a deploy (FR-8).

Code only: every factor is a template filled from tool results (diff rules, catalog, deploy
record) and cites the evidence it came from; the band is a rubric over the factors, never a
model's number. A signal that could not be checked is a factor of its own ("not checked"),
so a report is never LOW by silence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

RiskBand = Literal["LOW", "MEDIUM", "HIGH"]
# "context" is shown to the reader and never moves the band
Severity = Literal["context", "MEDIUM", "HIGH"]
FactorKind = Literal[
    "code_mechanism",  # a query / remote call added inside a loop
    "breaking_change",  # a field removed or renamed that other services still read
    "limit_change",  # a timeout / retries / pool constant changed (routine: context)
    "blast_radius",  # tier + dependents of the service (context; raises code_mechanism)
    "config_change",  # config keys the deploy changed (context)
    "not_checked",  # something could not be checked: unknown is not safe
    "nothing_shipped",  # a redeploy of the running commit or a rollback
]
ROLLBACK_KINDS = ("flag_off", "redeploy", "flag_not_read", "none")


TargetKind = Literal["pull_request", "range", "deploy"]
SHA = r"^[0-9a-f]{40}$"


class ReleaseRiskRequest(BaseModel):
    """Exactly one target:
    - pull_request: repository + pull_request (its merge commit)
    - range: a release CANDIDATE before it ships - service_key + base_sha (what runs now) +
      head_sha (what would ship); commits after base up to head
    - deploy: deploy_key - what that deploy shipped since the previous successful deploy of the
      service, plus its config changes ("would this have been flagged?")"""

    model_config = ConfigDict(extra="forbid")

    repository: str | None = Field(default=None, pattern=r"^[a-z0-9-]+/[a-z0-9-]+$", max_length=120)
    pull_request: int | None = Field(default=None, ge=1, le=10_000_000)
    service_key: str | None = Field(default=None, pattern=r"^[a-z0-9-]{2,100}$")
    base_sha: str | None = Field(default=None, pattern=SHA)
    head_sha: str | None = Field(default=None, pattern=SHA)
    deploy_key: str | None = Field(default=None, pattern=r"^DEPLOY-[0-9]{1,24}$")

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        pr = self.repository is not None or self.pull_request is not None
        rng = any(x is not None for x in (self.service_key, self.base_sha, self.head_sha))
        if pr and (self.repository is None or self.pull_request is None):
            raise ValueError("a pull request needs both repository and pull_request")
        if rng and None in (self.service_key, self.base_sha, self.head_sha):
            raise ValueError("a range needs service_key, base_sha and head_sha")
        if rng and self.base_sha == self.head_sha:
            raise ValueError("base_sha and head_sha are the same commit: nothing would ship")
        if sum((pr, rng, self.deploy_key is not None)) != 1:
            raise ValueError("give exactly one target: repository + pull_request, "
                             "service_key + base_sha + head_sha, or deploy_key")  # fmt: skip
        return self

    @property
    def kind(self) -> TargetKind:
        if self.deploy_key:
            return "deploy"
        return "range" if self.service_key else "pull_request"

    def label(self) -> str:
        if self.deploy_key:
            return self.deploy_key
        if self.service_key and self.base_sha and self.head_sha:
            return f"{self.service_key} {self.base_sha[:7]}..{self.head_sha[:7]}"
        return f"{self.repository}#{self.pull_request}"


class ReleaseRiskTask(BaseModel):
    """POST /v1/agents/release_risk/run (orchestrator -> agents), synchronous, AS THE USER."""

    model_config = ConfigDict(extra="forbid")

    assessment_id: UUID
    request: ReleaseRiskRequest


class RiskFactor(BaseModel):
    kind: FactorKind
    severity: Severity
    statement: str = Field(max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class RollbackStep(BaseModel):
    """How to take the risky part back OUT, decided by code from the diff and the deploy:
    flag_off (the risky code is behind a flag), redeploy (it is not: only a rollback removes
    it), flag_not_read (a flag this deploy changed is not read by the changed code: switching
    it back does NOT undo the code change), none (nothing risky to take back)."""

    kind: Literal["flag_off", "redeploy", "flag_not_read", "none"]
    statement: str = Field(max_length=400)


class ReleaseRiskReport(BaseModel):
    """`band` = the advice: risk found, raised to at least MEDIUM when part of the release could
    not be checked (unknown is not safe). `findings_band` = risk found in what WAS checked, and
    `coverage` says whether everything was - so "we found a problem" and "we could not see" are
    never confused (the scan showed the second one dominating on the synthetic history)."""

    target_kind: TargetKind
    service_key: str | None = None
    repository: str | None = None
    pull_request: int | None = None
    deploy_key: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    band: RiskBand | None = None  # None only when the target could not be resolved (FAILED)
    findings_band: RiskBand | None = None
    coverage: Literal["full", "partial"] = "full"
    factors: list[RiskFactor] = Field(default_factory=list, max_length=40)
    rollback: list[RollbackStep] = Field(default_factory=list, max_length=20)
    checked: list[str] = Field(default_factory=list, max_length=10, description="signals assessed")
    not_checked: list[str] = Field(default_factory=list, max_length=20)
    advisory: Literal[True] = True  # FR-8: never blocks a deploy; nothing reads this to gate


class EvidenceOut(BaseModel):
    evidence_key: str
    kind: str
    title: str
    excerpt: str
    uri: str | None = None


class ReleaseRiskOut(BaseModel):
    """GET /api/v1/release-risk/{id}. Readable with code:read (it holds code excerpts)."""

    id: UUID
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"]
    band: RiskBand | None
    requested_by: UUID
    requested_at: datetime
    report: ReleaseRiskReport | None
    evidence: list[EvidenceOut] = Field(default_factory=list)
    error: str | None = None
    agent_version: str | None = None
