"""Search / documents contract (rag service, exposed via the api gateway as /api/v1/search)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
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
    """Keyword search over closed historical incidents (vector similarity: Phase 13)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    service_key: str | None = Field(default=None, max_length=100)
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
