"""Search / documents contract (rag service, exposed via the api gateway as /api/v1/search)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SearchMode(StrEnum):
    HYBRID = "hybrid"  # vector + keyword, fused with RRF (default)
    VECTOR = "vector"  # ablation / debugging
    KEYWORD = "keyword"  # ablation / debugging


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[str] | None = Field(default=None, max_length=8)
    departments: list[str] | None = Field(default=None, max_length=20)


class SearchRequest(BaseModel):
    """The caller's groups come from the verified token - never from this body."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=8, ge=1, le=20)
    mode: SearchMode = SearchMode.HYBRID
    rerank: bool | None = Field(
        default=None, description="null = service default (off until the eval shows a gain)"
    )
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    title: str
    source: str
    source_uri: str
    chunk_index: int
    content: str
    rank: int
    rrf_score: float
    vector_rank: int | None = None
    keyword_rank: int | None = None
    similarity: float | None = None
    rerank_relevance: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RerankInfo(BaseModel):
    requested: bool
    applied: bool
    route: str | None = None
    model: str | None = None
    fallback_used: bool = False
    latency_ms: int | None = None
    error: str | None = None


class SearchResponse(BaseModel):
    retrieval_id: UUID
    mode: SearchMode
    embedding_model: str | None
    results: list[SearchHit]
    rerank: RerankInfo
    timings_ms: dict[str, int]
    degraded: str | None = Field(
        default=None, description="set when part of the pipeline was skipped"
    )


class DocumentOut(BaseModel):
    id: UUID
    title: str
    source: str
    source_uri: str
    department: str
    owner: str
    version: str
    sensitivity: str
    content: str
    indexed_at: datetime | None
    chunk_count: int


class IncidentSearchRequest(BaseModel):
    """Similar past incidents (Phase 7 keyword; Phase 13 hybrid keyword + vector, ADR-023)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    # Phase 7: a FILTER. Phase 13 (`boost_service`): a ranking boost instead, so a similar
    # incident on another service still shows (pool exhaustion on orders-api is relevant)
    service_key: str | None = Field(default=None, max_length=100)
    boost_service: bool = False
    mode: Literal["hybrid", "keyword", "vector"] = "keyword"  # default = Phase 7 behaviour
    exclude_key: str | None = Field(default=None, pattern=r"^INC-[0-9]+$")  # eval hold-out
    # only incidents shared with every engineer (`eng-all`): what an agent may store on another
    # incident. Restricted ones are filtered IN the SQL, so they are never read (ADR-023)
    shared_only: bool = False
    k: int = Field(default=5, ge=1, le=20)


class HistoricalIncidentHit(BaseModel):
    incident_key: str
    title: str
    summary: str
    root_cause: str
    root_cause_category: str
    remediation: str
    service_keys: list[str]
    severity: str
    occurred_at: datetime
    resolved_at: datetime
    score: float
    # Phase 13: which branch found it (None = not in that branch's top list)
    rank_k: int | None = None
    rank_v: int | None = None
    retrieval: Literal["hybrid", "keyword", "vector"] = "keyword"
    # shared with every engineer (`eng-all`): only such incidents may be stored on another
    # incident's record (ADR-023)
    open_to_engineering: bool = False


# ------------------------------------------------------------------ code (Phase 14, ADR-024)
class CodeReferencesRequest(BaseModel):
    """Who uses a symbol? `ShipmentResponse.address` (a member of a type: also matches
    getAddress / Address, in services that use the type) or a plain name (`findOpenOrders`)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=2, max_length=200, pattern=r"^[A-Za-z_][\w.]*$")
    exclude_repository: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=50, ge=1, le=200)


class CodeReferenceHit(BaseModel):
    repository: str
    path: str
    service_key: str | None
    line: int
    kind: Literal["call", "type", "field"]
    name: str
    enclosing: str | None
    open_to_engineering: bool = False
