"""Phase 14 (ADR-024): what a deploy changed in code, found by CODE rules over the diffs.

No model writes any of this: every statement is a template filled from the diff and the
tree-sitter parse. A finding cites the COMMIT evidence of the commit it came from; an impact
reference cites CODE evidence (a location at HEAD).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from aeoi_models.findings import EvidenceId

CodeRule = Literal["query_in_loop", "remote_call_in_loop", "limit_changed", "field_removed"]
# rules that describe a LOAD mechanism on the deployed service itself (one round trip per
# item): the only findings that become observations and may support their deploy. A changed
# limit is reported but is not support (review: it fired on 883 of 1784 synthetic deploys -
# routine tuning, not evidence); a removed field breaks OTHER services (impact).
MECHANISM_RULES: frozenset[str] = frozenset({"query_in_loop", "remote_call_in_loop"})


class ImpactRef(BaseModel):
    service_key: str | None
    repository: str
    path: str
    line: int
    name: str
    evidence_id: EvidenceId


class CodeFinding(BaseModel):
    rule: CodeRule
    deploy_key: str
    deploy_started_at: datetime
    service_key: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    committed_at: datetime
    pull_request: int | None = None
    path: str
    line: int = Field(ge=0)
    symbol: str = Field(max_length=200)
    statement: str = Field(max_length=500)
    evidence_id: EvidenceId
    impact: list[ImpactRef] = Field(default_factory=list, max_length=50)


class CodeStats(BaseModel):
    deploys_checked: int = 0
    commits_checked: int = 0
    files_checked: int = 0
    files_skipped: int = 0  # too long for the gateway, or the diff does not fit the file
    findings: int = 0
    impact_refs: int = 0
    not_shared: int = 0  # references in code not shared with all engineers (not kept)
    base_missing: int = 0  # no previous deploy commit: only the head commit was checked
