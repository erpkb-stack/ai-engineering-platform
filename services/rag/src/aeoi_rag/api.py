"""HTTP API. Callers are USERS (via the api gateway, which forwards their token).

Groups for the permission filter come only from the verified token. A service that searches
on behalf of a user (agents, Phase 8) forwards that user's token; a service token alone has
no groups and therefore sees nothing (no confused deputy).
"""

from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import NotFoundError
from aeoi_db.models.rag import Document, DocumentChunk
from aeoi_models.api.search import (
    DocumentOut,
    HistoricalIncidentHit,
    IncidentSearchRequest,
    RerankInfo,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from aeoi_rag.ingest import new_retrieval_id
from aeoi_rag.rerank import LLMReranker
from aeoi_rag.search import Retriever
from aeoi_security.auth import Principal
from aeoi_security.rbac import Perm
from aeoi_web import require_acting_user

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["search"])
# ADR-020: a user token, or the tool-gateway's service token (`rag:obo`) + the user's token
# (or an investigation's delegated token) in X-On-Behalf-Of. Either way `principal` is the
# USER, and their groups - never the service's - go into the SQL filter.
RAG_OBO_SCOPE = "rag:obo"
Reader = Annotated[Principal, Depends(require_acting_user(Perm.DOCS_READ, RAG_OBO_SCOPE))]


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, principal: Reader, request: Request) -> SearchResponse:
    retriever: Retriever = request.app.state.retriever
    reranker: LLMReranker | None = request.app.state.reranker
    want_rerank = (
        body.rerank if body.rerank is not None else request.app.state.settings.rerank_default
    )
    result = await retriever.search(
        body.query, sorted(principal.groups), k=body.k, mode=body.mode, filters=body.filters
    )
    hits = result.hits
    info = RerankInfo(requested=want_rerank, applied=False)
    timings = dict(result.timings_ms)
    if want_rerank and reranker is not None:
        out = await reranker.rerank(body.query, hits)
        hits = out.hits
        info = RerankInfo(
            requested=True,
            applied=out.applied,
            route=out.route,
            model=out.model,
            fallback_used=out.fallback_used,
            latency_ms=out.latency_ms,
            error=out.error,
        )
        timings["rerank"] = out.latency_ms or 0
        timings["total"] = timings.get("total", 0) + (out.latency_ms or 0)
    retrieval_id = new_retrieval_id()
    # Audit-friendly log: who, how many, which chunks. The query text itself may be sensitive:
    # only its hash and length are logged.
    log.info(
        "rag_search",
        retrieval_id=str(retrieval_id),
        actor=principal.actor,
        via=getattr(request.state, "via", None),
        delegation_grant=(
            str(principal.delegation.grant_id) if principal.delegation is not None else None
        ),
        query_sha=hashlib.sha256(body.query.encode()).hexdigest()[:16],
        query_len=len(body.query),
        mode=body.mode.value,
        results=len(hits),
        reranked=info.applied,
        chunk_ids=[str(h.chunk_id) for h in hits],
        timings_ms=timings,
    )
    return SearchResponse(
        retrieval_id=retrieval_id,
        mode=body.mode,
        embedding_model=result.embedding_model,
        results=[
            SearchHit(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                title=h.title,
                source=h.source,
                source_uri=h.source_uri,
                chunk_index=h.chunk_index,
                content=h.content,
                rank=i + 1,
                rrf_score=round(h.rrf_score, 6),
                vector_rank=h.vector_rank,
                keyword_rank=h.keyword_rank,
                similarity=round(h.similarity, 4) if h.similarity is not None else None,
                rerank_relevance=h.rerank_relevance,
                metadata=h.metadata,
            )
            for i, h in enumerate(hits)
        ],
        rerank=info,
        timings_ms=timings,
        degraded=result.degraded,
    )


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID,
    principal: Annotated[
        Principal,
        Depends(require_acting_user(Perm.DOCS_READ, RAG_OBO_SCOPE, allow_delegated=False)),
    ],
    request: Request,
) -> DocumentOut:
    """404 - not 403 - when the caller may not see it: existence is information too."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessions() as s:
        doc = await s.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.allowed_groups.overlap(sorted(principal.groups)),
                ~Document.quarantined,
            )
        )
        if doc is None:
            raise NotFoundError("Document not found.")
        n = await s.scalar(select(func.count()).where(DocumentChunk.document_id == doc.id)) or 0
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        source=doc.source,
        source_uri=doc.source_uri,
        department=doc.department,
        owner=doc.owner,
        version=doc.version,
        sensitivity=doc.sensitivity,
        content=doc.content,
        indexed_at=doc.indexed_at,
        chunk_count=n,
    )


_INCIDENT_SQL = text("""
WITH q AS (
  SELECT CASE WHEN plainto_tsquery('english', :q)::text = '' THEN NULL
              ELSE replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery END AS tsq
)
SELECT h.incident_key, h.title, h.summary, h.root_cause, h.root_cause_category, h.remediation,
       h.service_keys, h.severity, h.occurred_at, h.resolved_at, ts_rank_cd(h.tsv, q.tsq) AS score
FROM rag.historical_incidents h, q
WHERE q.tsq IS NOT NULL AND h.tsv @@ q.tsq
  AND h.allowed_groups && CAST(:groups AS varchar[])
  AND (CAST(:svc AS text) IS NULL OR CAST(:svc AS text) = ANY(h.service_keys))
ORDER BY score DESC, h.occurred_at DESC
LIMIT :k
""")


@router.post("/incidents/search", response_model=list[HistoricalIncidentHit])
async def search_incidents(
    body: IncidentSearchRequest,
    principal: Annotated[
        Principal, Depends(require_acting_user(Perm.INCIDENTS_READ, RAG_OBO_SCOPE))
    ],
    request: Request,
) -> list[HistoricalIncidentHit]:
    """Keyword search over past incidents with the same group ACL rule as documents."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessions() as s:
        rows = (await s.execute(_INCIDENT_SQL, {
            "q": body.query, "groups": sorted(principal.groups), "svc": body.service_key, "k": body.k,
        })).mappings().all()  # fmt: skip
    return [HistoricalIncidentHit(**{**r, "score": float(r["score"])}) for r in rows]
