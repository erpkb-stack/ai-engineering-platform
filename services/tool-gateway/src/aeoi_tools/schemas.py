"""Input/output contracts for every tool. Rules (contract-lint test enforces them):
- every string has max_length, every list has max_length, extra fields are forbidden;
- time ranges are timezone-aware and at most MAX_WINDOW long;
- every output has `items`, and every item has `evidence_id` (filled by the gateway) so a
  finding can cite exactly which tool result supports it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_WINDOW = timedelta(hours=24)
SERVICE_KEY = r"^[a-z0-9][a-z0-9-]{1,99}$"
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")  # severity order
METRICS = (
    "http_5xx_per_min",
    "p95_latency_ms",
    "requests_per_sec",
    "cpu_util",
    "db_pool_utilization",
    "db_connections_active",
    "db_query_latency_ms",
    "cache_hit_ratio",
    "thread_pool_utilization",
)


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


class In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _no_nul(cls, data: Any) -> Any:
        # NUL can't be stored in Postgres text/JSONB: reject it as bad input (recorded 422)
        # instead of failing later inside the query or, worse, inside the audit write.
        if _has_nul(data):
            raise ValueError("NUL characters are not allowed")
        return data


class Item(BaseModel):
    evidence_id: str = Field(default="", max_length=80, description="assigned by the gateway")


class Out(BaseModel):
    truncated: bool = False


class TimeWindow(In):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must include a timezone (e.g. 2026-10-02T09:00:00Z)")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError(f"window larger than {MAX_WINDOW}; narrow it (cost + blast radius)")
        return self


# ------------------------------------------------------------------ catalog
class CatalogIn(In):
    service_key: str | None = Field(default=None, pattern=SERVICE_KEY, max_length=100)
    team: str | None = Field(default=None, max_length=80)
    name_contains: str | None = Field(default=None, min_length=2, max_length=60)

    @model_validator(mode="after")
    def _one_filter(self) -> Self:
        if not (self.service_key or self.team or self.name_contains):
            raise ValueError("give service_key, team or name_contains (no full dumps)")
        return self


class ServiceItem(Item):
    key: str
    name: str
    type: str
    tier: int
    team: str
    language: str | None = None
    repository: str | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    dependents: list[str] = Field(default_factory=list, max_length=200)


class CatalogOut(Out):
    items: list[ServiceItem] = Field(default_factory=list, max_length=200)


# ------------------------------------------------------------------ logs
class LogsIn(TimeWindow):
    service_key: str = Field(pattern=SERVICE_KEY, max_length=100)
    min_level: Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"] = "WARN"
    error_code: str | None = Field(default=None, max_length=60, pattern=r"^[A-Za-z0-9_.-]+$")
    contains: str | None = Field(default=None, min_length=2, max_length=100)
    order: Literal["asc", "desc"] = Field(
        default="asc", description="asc = onset first (default); desc = most recent first"
    )
    limit: int = Field(default=100, ge=1, le=200)


class LogItem(Item):
    ts: datetime
    level: str
    message: str
    error_code: str | None = None  # log DATA: untrusted, may hold any text
    trace_id: str | None = None


class LogsOut(Out):
    items: list[LogItem] = Field(default_factory=list, max_length=200)
    total_matching: int = 0  # exact, whole window
    distinct_codes: int = 0  # exact; counts_by_error_code holds only the top 50
    counts_by_error_code: dict[str, int] = Field(default_factory=dict)


# ------------------------------------------------------------------ metrics
class MetricsIn(TimeWindow):
    service_key: str = Field(pattern=SERVICE_KEY, max_length=100)
    metric: Literal[METRICS]  # type: ignore[valid-type]
    max_points: int = Field(default=200, ge=10, le=500)


class MetricPointOut(BaseModel):
    ts: datetime
    value: float


class MetricSeriesItem(Item):
    service_key: str
    metric: str
    bucket_seconds: int
    points: list[MetricPointOut] = Field(default_factory=list, max_length=500)
    stats: dict[str, float | None] = Field(default_factory=dict)


class MetricsOut(Out):
    items: list[MetricSeriesItem] = Field(default_factory=list, max_length=1)


# ------------------------------------------------------------------ deployments
class DeploymentsIn(In):
    deploy_key: str | None = Field(default=None, pattern=r"^DEPLOY-[0-9]{1,10}$")
    service_key: str | None = Field(default=None, pattern=SERVICE_KEY, max_length=100)
    start: datetime | None = None
    end: datetime | None = None
    environment: Literal["development", "staging", "production"] = "production"
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not self.deploy_key and not self.service_key:
            raise ValueError("give deploy_key, or service_key with a time window")
        if self.service_key and not self.deploy_key:
            if not (self.start and self.end):
                raise ValueError("service_key needs start and end")
            TimeWindow(start=self.start, end=self.end)  # same rules as other tools
        return self


class DeploymentItem(Item):
    deploy_key: str
    service_key: str
    version: str
    environment: str
    status: str
    commit_sha: str
    deployed_by: str
    started_at: datetime
    finished_at: datetime | None = None
    config_changed_keys: list[str] = Field(default_factory=list, max_length=100)
    # Phase 14: the commit the PREVIOUS successful deploy of this service shipped - the
    # commits after it, up to commit_sha, are what this deploy changed (compare_commits)
    previous_commit_sha: str | None = None


class DeploymentsOut(Out):
    items: list[DeploymentItem] = Field(default_factory=list, max_length=50)


class ConfigDiffIn(In):
    deploy_key: str = Field(pattern=r"^DEPLOY-[0-9]{1,10}$")


class ConfigChange(BaseModel):
    key: str
    before: Any = None
    after: Any = None


class ConfigDiffItem(Item):
    deploy_key: str
    service_key: str
    version: str
    changes: list[ConfigChange] = Field(default_factory=list, max_length=200)


class ConfigDiffOut(Out):
    items: list[ConfigDiffItem] = Field(default_factory=list, max_length=1)


# ------------------------------------------------------------------ code
class CommitIn(In):
    sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class CommitItem(Item):
    sha: str
    repository: str
    service_key: str
    author: str
    message: str
    files_changed: list[str] = Field(default_factory=list, max_length=500)
    additions: int
    deletions: int
    committed_at: datetime


class CommitOut(Out):
    items: list[CommitItem] = Field(default_factory=list, max_length=1)


class CompareCommitsIn(In):
    """Phase 14 (ADR-024): the commits a deploy shipped, with their diffs (a code host's
    "compare" API): commits of `service_key` after `base_sha` up to and including `head_sha`."""

    service_key: str = Field(pattern=SERVICE_KEY, max_length=100)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    limit: int = Field(default=10, ge=1, le=20)


class FileDiff(BaseModel):
    """`truncated`: the file or its diff is longer than the gateway passes on (the sanitizer
    cuts every string at 4000 chars); then BOTH texts are empty - a cut file would be parsed
    wrong, so the agent reports it as unchecked instead (review finding)."""

    path: str
    patch: str = Field(max_length=4_000)
    content_after: str = Field(max_length=4_000)
    additions: int
    deletions: int
    truncated: bool = False


class CommitDiffItem(Item):
    sha: str
    repository: str
    service_key: str
    author: str
    message: str
    committed_at: datetime
    pull_request: int | None = None
    files: list[FileDiff] = Field(default_factory=list, max_length=20)


CompareRelation = Literal["ahead", "same", "rollback", "no_base", "head_unknown"]


class CompareCommitsOut(Out):
    """`relation` of head to base: ahead (commits after base up to head), same (a redeploy of
    the same commit: nothing new shipped), rollback (head is older than base: nothing new
    shipped), no_base (base not given / not found: the head commit only), head_unknown.
    `more`: there were more commits in the range than `limit`."""

    items: list[CommitDiffItem] = Field(default_factory=list, max_length=20)
    base_found: bool = True
    relation: CompareRelation = "ahead"
    more: bool = False


class FindReferencesIn(In):
    """Phase 14 (ADR-024): who uses a symbol, across every repository the user may read."""

    symbol: str = Field(min_length=2, max_length=200, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    exclude_repository: str | None = Field(
        default=None, pattern=r"^[a-z0-9-]+/[a-z0-9-]+$", max_length=120
    )
    limit: int = Field(default=50, ge=1, le=100)


class CodeRefItem(Item):
    repository: str
    path: str
    service_key: str | None = None
    line: int
    kind: Literal["call", "type", "field"]
    name: str
    enclosing: str | None = None
    open_to_engineering: bool = False


class FindReferencesOut(Out):
    items: list[CodeRefItem] = Field(default_factory=list, max_length=100)


class PullRequestIn(In):
    repository: str = Field(pattern=r"^[a-z0-9-]+/[a-z0-9-]+$", max_length=120)
    number: int = Field(ge=1, le=10_000_000)


class PullRequestItem(Item):
    repository: str
    number: int
    title: str
    body: str
    author: str
    state: str
    changed_files: list[str] = Field(default_factory=list, max_length=500)
    merge_commit_sha: str | None = None
    created_at: datetime
    merged_at: datetime | None = None


class PullRequestOut(Out):
    items: list[PullRequestItem] = Field(default_factory=list, max_length=1)


class RepoSearchIn(In):
    query: str = Field(min_length=2, max_length=100)
    repository: str | None = Field(default=None, pattern=r"^[a-z0-9-]+/[a-z0-9-]+$", max_length=120)
    service_key: str | None = Field(default=None, pattern=SERVICE_KEY, max_length=100)
    since: datetime | None = None
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if not (self.repository or self.service_key):
            raise ValueError("give repository or service_key (no org-wide code search)")
        return self


class RepoHitItem(Item):
    kind: Literal["commit", "pull_request"]
    ref: str  # sha or repo#number
    repository: str
    title: str
    author: str
    at: datetime
    files: list[str] = Field(default_factory=list, max_length=100)


class RepoSearchOut(Out):
    items: list[RepoHitItem] = Field(default_factory=list, max_length=50)


# ------------------------------------------------------------------ knowledge (via rag)
class DocSearchIn(In):
    query: str = Field(min_length=2, max_length=500)
    k: int = Field(default=5, ge=1, le=10)
    sources: list[Literal["markdown", "pdf", "txt", "html", "json", "code", "runbook"]] | None = (
        Field(default=None, max_length=7)
    )


class RunbookSearchIn(In):
    query: str = Field(min_length=2, max_length=500)
    k: int = Field(default=5, ge=1, le=10)


class DocHitItem(Item):
    document_id: str
    chunk_id: str
    title: str
    source: str
    source_uri: str
    content: str
    rank: int


class DocSearchOut(Out):
    items: list[DocHitItem] = Field(default_factory=list, max_length=10)
    embedding_model: str | None = None
    degraded: str | None = None


class IncidentSearchIn(In):
    query: str = Field(min_length=2, max_length=500)
    service_key: str | None = Field(default=None, pattern=SERVICE_KEY, max_length=100)
    # Phase 13 (ADR-023): hybrid keyword + vector; the service is a ranking boost, not a
    # filter (a similar incident on another service is still relevant). Defaults = Phase 7.
    mode: Literal["keyword", "hybrid"] = "keyword"
    boost_service: bool = False
    shared_only: bool = False  # only incidents every engineer may read (ADR-023)
    k: int = Field(default=5, ge=1, le=10)


class IncidentHitItem(Item):
    incident_key: str
    title: str
    summary: str
    root_cause: str
    root_cause_category: str
    remediation: str
    service_keys: list[str] = Field(default_factory=list, max_length=50)
    severity: str
    occurred_at: datetime
    resolved_at: datetime
    rank_k: int | None = None
    rank_v: int | None = None
    retrieval: Literal["hybrid", "keyword", "vector"] = "keyword"
    open_to_engineering: bool = False


class IncidentSearchOut(Out):
    items: list[IncidentHitItem] = Field(default_factory=list, max_length=10)


# ------------------------------------------------------------------ consequential
class RollbackIn(In):
    deploy_key: str = Field(pattern=r"^DEPLOY-[0-9]{1,10}$")
    reason: str = Field(min_length=10, max_length=500)


class RollbackItem(Item):
    deploy_key: str
    rolled_back_to: str


class RollbackOut(Out):
    items: list[RollbackItem] = Field(default_factory=list, max_length=1)
