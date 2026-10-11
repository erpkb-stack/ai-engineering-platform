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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import NotFoundError, UpstreamUnavailableError
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
from aeoi_rag import history
from aeoi_rag.embedder import Embedder
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


@router.post("/incidents/search", response_model=list[HistoricalIncidentHit])
async def search_incidents(
    body: IncidentSearchRequest,
    principal: Annotated[
        Principal, Depends(require_acting_user(Perm.INCIDENTS_READ, RAG_OBO_SCOPE))
    ],
    request: Request,
) -> list[HistoricalIncidentHit]:
    """Similar past incidents; the group ACL is in the SQL, groups come from the token.
    `mode=keyword` (default) is the Phase 7 search. `hybrid`/`vector` add the pgvector branch
    (ADR-023); if the embedder fails, hybrid degrades to keyword and every hit says so
    (`retrieval`), vector-only fails loudly (503)."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    embedder: Embedder = request.app.state.embedder
    qvec: list[float] | None = None
    retrieval = body.mode
    if body.mode != "keyword":
        try:
            qvec = await embedder.embed_query(body.query)
        except Exception as e:
            if body.mode == "vector":
                raise UpstreamUnavailableError("embedding service unavailable") from e
            log.warning("incident_search_vector_unavailable", error=type(e).__name__)
            retrieval = "keyword"
    rows = await history.search(
        sessions,
        body.query,
        sorted(principal.groups),
        k=body.k,
        qvec=qvec,
        model=embedder.model if qvec is not None else None,
        service_key=body.service_key if body.boost_service else None,
        only_service=None if body.boost_service else body.service_key,
        use_keyword=body.mode != "vector",
        exclude=body.exclude_key,
        shared_only=body.shared_only,
        iterative_scan=request.app.state.retriever._iterative_scan,
    )
    if qvec is not None and rows and not rows[0]["vec_hits"]:
        # the vector branch found nothing at all: no vectors for this embedding model yet
        # (`make embed-history`). Say so instead of labelling keyword hits "hybrid".
        log.warning("incident_search_no_vectors", model=embedder.model)
        retrieval = "keyword"
    return [HistoricalIncidentHit(**r, retrieval=retrieval) for r in rows]
