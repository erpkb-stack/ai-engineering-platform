"""Phase 13 (ADR-023): similar past incidents, compared by typed claims, and history priors.

Only past incidents shared with every engineer (`eng-all`) are kept: these objects are stored
on ANOTHER incident (agent output, evidence, hypotheses, postmortem), where anyone who may read
that incident sees them. They carry no free text from the past incident - only its key, its
root-cause CATEGORY (a closed vocabulary) and claims checked by code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from aeoi_models.findings import EvidenceId

ClaimField = Literal["service", "error_code", "symptom", "deploy_before"]
Relation = Literal["same", "different"]
# symptoms code can see BOTH in this incident's observations and in a past incident's text
Symptom = Literal["errors", "latency", "connection_pool", "thread_pool", "cache", "cpu"]
IncidentKey = str  # "INC-1234"


class Claim(BaseModel):
    """'This incident and INC-x have the same/different <field>'. Proposed by the model (or by
    code when the model is down); KEPT only if code computes the same relation."""

    field: ClaimField
    relation: Relation
    value: str = Field(default="", max_length=80, description="code-written: the code/symptom")


class SimilarIncident(BaseModel):
    incident_key: IncidentKey = Field(pattern=r"^INC-[0-9]{1,12}$")
    rank: int = Field(ge=1)
    retrieval: Literal["hybrid", "keyword", "vector"]
    rank_keyword: int | None = None
    rank_vector: int | None = None
    root_cause_category: str = Field(max_length=64)
    service_keys: list[str] = Field(default_factory=list, max_length=10)
    occurred_at: datetime
    evidence_id: EvidenceId
    claims: list[Claim] = Field(default_factory=list, max_length=8)
    compared_by: Literal["llm", "code"] = "code"
    # "service": from the search of THIS incident's own service; "all": the same failure anywhere
    scope: Literal["service", "all"] = "all"


class HistoryPrior(BaseModel):
    """'N of M similar past incidents had a cause of this kind.' A base rate, NOT evidence
    about this incident: it never changes a band; it only breaks a tie inside one."""

    cause_kind: Literal["change", "saturation", "traffic"]
    n: int = Field(ge=0)
    of: int = Field(ge=0)
    categories: dict[str, int] = Field(default_factory=dict)
    incident_keys: list[IncidentKey] = Field(default_factory=list, max_length=10)
    statement: str = Field(max_length=300)


class HistoryStats(BaseModel):
    searched: int = 0  # hits the search returned for this user
    not_shared: int = 0  # dropped: not shared with every engineer (never stored)
    claims_proposed: int = 0
    claims_kept: int = 0
    claims_rejected: int = 0  # contradicted by code, or not checkable
    retrieval: Literal["hybrid", "keyword", "vector"] | None = None
