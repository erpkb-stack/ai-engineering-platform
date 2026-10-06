"""Permission-aware hybrid retrieval: vector (HNSW) + keyword (FTS) fused with RRF, in ONE SQL
statement, with the caller's groups applied INSIDE each candidate branch (ADR-006).

Why the filter sits inside each branch, before LIMIT: filtering after "top 40 nearest" would
(a) return fewer than k results for users with narrow access and (b) still have touched
restricted rows. Here a restricted chunk is never a candidate for a caller without access.

Why RRF instead of adding scores: cosine similarity and ts_rank live on different scales;
RRF uses only ranks (1/(k+rank)), needs no tuning per corpus, and is hard to get wrong.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import UpstreamUnavailableError
from aeoi_db.base import EMBEDDING_DIM
from aeoi_llm_client import LLMError
from aeoi_models.api.search import SearchFilters, SearchMode
from aeoi_rag.config import Settings
from aeoi_rag.embedder import Embedder, EmbeddingError

# Each branch: ORDER BY + LIMIT first (lets HNSW / GIN do the work), number the rows after.
_SQL = """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY dist) AS rank_v, 1 - dist AS sim FROM (
    SELECT c.id, c.embedding <=> CAST(:qvec AS vector) AS dist
    FROM rag.document_chunks c JOIN rag.documents d ON d.id = c.document_id
    WHERE c.allowed_groups && CAST(:groups AS varchar[])
      AND NOT d.quarantined
      AND c.embedding IS NOT NULL AND c.embedding_model = :model
      AND (CAST(:sources AS text[]) IS NULL OR d.source = ANY(CAST(:sources AS text[])))
      AND (CAST(:departments AS text[]) IS NULL OR d.department = ANY(CAST(:departments AS text[])))
    ORDER BY c.embedding <=> CAST(:qvec AS vector)
    LIMIT :nv
  ) v
), q AS (
  -- OR semantics: natural-language questions rarely contain ALL their words in one chunk.
  SELECT CASE WHEN plainto_tsquery('english', :q)::text = '' THEN NULL
              ELSE replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery END AS tsq,
         CASE WHEN CAST(:ident AS text) IS NULL THEN NULL
              ELSE plainto_tsquery('english', CAST(:ident AS text)) END AS ident
), kw AS (
  SELECT id, row_number() OVER (ORDER BY kscore DESC, id) AS rank_k, kscore FROM (
    -- ts_rank_cd has no IDF: a frequent word ("tls") outranks a rare error code ("2535").
    -- Chunks containing ALL identifier-like query terms get +1 (see identifier_terms()).
    SELECT c.id, ts_rank_cd(c.tsv, q.tsq)
             + CASE WHEN q.ident IS NOT NULL AND c.tsv @@ q.ident THEN 1.0 ELSE 0.0 END AS kscore
    FROM rag.document_chunks c JOIN rag.documents d ON d.id = c.document_id, q
    WHERE q.tsq IS NOT NULL AND c.tsv @@ q.tsq
      AND c.allowed_groups && CAST(:groups AS varchar[])
      AND NOT d.quarantined
      AND (CAST(:sources AS text[]) IS NULL OR d.source = ANY(CAST(:sources AS text[])))
      AND (CAST(:departments AS text[]) IS NULL OR d.department = ANY(CAST(:departments AS text[])))
    ORDER BY kscore DESC, c.id
    LIMIT :nk
  ) k
), fused AS (
  SELECT coalesce(vec.id, kw.id) AS id, vec.rank_v, kw.rank_k, vec.sim, kw.kscore,
         :wv * coalesce(1.0 / (:rrf_k + vec.rank_v), 0)
         + :wk * coalesce(1.0 / (:rrf_k + kw.rank_k), 0) AS rrf
  FROM vec FULL OUTER JOIN kw ON vec.id = kw.id
)
SELECT f.id AS chunk_id, f.rank_v, f.rank_k, f.sim, f.rrf,
       c.document_id, c.chunk_index, c.content, c.metadata, c.allowed_groups,
       d.title, d.source, d.source_uri, d.sensitivity
FROM fused f
JOIN rag.document_chunks c ON c.id = f.id
JOIN rag.documents d ON d.id = c.document_id
ORDER BY f.rrf DESC, f.id
LIMIT :n
"""


@dataclass
class Hit:
    chunk_id: UUID
    document_id: UUID
    title: str
    source: str
    source_uri: str
    sensitivity: str
    chunk_index: int
    content: str
    rrf_score: float
    vector_rank: int | None
    keyword_rank: int | None
    similarity: float | None
    allowed_groups: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    rerank_relevance: int | None = None


@dataclass(frozen=True)
class Candidate:
    chunk_id: UUID
    document_id: UUID
    source_uri: str
    rank_v: int | None
    rank_k: int | None


def fuse(
    cands: Sequence[Candidate],
    *,
    rrf_k: int,
    wv: float,
    wk: float,
    depth: int,
    max_per_doc: int,
    k: int,
) -> list[str]:
    """Python twin of the SQL fusion (same formula, same tie-break by chunk id), used by the
    sweep. An integration test asserts it returns the same order as `search()`."""
    scored = []
    for c in cands:
        rv = c.rank_v if c.rank_v is not None and c.rank_v <= depth else None
        rk = c.rank_k if c.rank_k is not None and c.rank_k <= depth else None
        if rv is None and rk is None:
            continue
        score = (wv / (rrf_k + rv) if rv else 0.0) + (wk / (rrf_k + rk) if rk else 0.0)
        if score == 0.0:  # a branch weighted to 0 contributes nothing (single-branch ablation)
            continue
        scored.append((-score, c.chunk_id, c))
    scored.sort(key=lambda t: (t[0], t[1]))
    scored = scored[: k * 3]  # same candidate pool as search() (LIMIT k * candidates_multiplier)
    per_doc: dict[UUID, int] = {}
    uris: list[str] = []
    for _, _, c in scored:
        if per_doc.get(c.document_id, 0) >= max_per_doc:
            continue
        per_doc[c.document_id] = per_doc.get(c.document_id, 0) + 1
        if c.source_uri not in uris:
            uris.append(c.source_uri)
        if sum(per_doc.values()) >= k:
            break
    return uris


@dataclass
class SearchResult:
    hits: list[Hit]
    embedding_model: str | None
    timings_ms: dict[str, int]
    degraded: str | None = None


_IDENT = re.compile(
    r"\b(?:[A-Za-z]+[-_]?)*\d[\w-]*\b"  # contains a digit: ERR-TLS-2535, INC-7312, v2
    r"|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b"  # camelCase: maxPoolSize
    r"|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"  # PascalCase: LedgerReconciler
    r"|\b[a-z0-9]+(?:_[a-z0-9]+)+\b"  # snake_case: compute_backoff_with_jitter
    r"|\b[A-Z]{4,}\b"  # CODENAMES, ACRONYMS
)


def identifier_terms(query: str) -> list[str]:
    """Identifier-like tokens: rare by construction, so an exact match should dominate.
    A heuristic stand-in for BM25's IDF (Postgres FTS has none); see ADR-016."""
    seen: list[str] = []
    for m in _IDENT.finditer(query):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def _vec_literal(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class Retriever:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        settings: Settings,
        *,
        iterative_scan: bool = False,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._s = settings
        self._iterative_scan = iterative_scan  # pgvector >= 0.8 (see detect_iterative_scan)

    def fusion_params(self, query: str) -> tuple[list[str], float, float]:
        """(identifier terms, vector weight, keyword weight) for this query.

        Query-type routing: a query with an identifier (error code, ticket, class name) is
        answered by an exact match - embeddings blur "2535" and "2534" - so keyword is weighted;
        a descriptive query is the vector branch's strength. Weights come from settings and are
        set from `make rag-sweep` (dev split), never by hand. 1.0/1.0 = plain RRF.
        """
        idents = identifier_terms(query)
        if idents:
            return idents, 1.0, self._s.keyword_weight_identifiers
        return idents, self._s.vector_weight_no_identifiers, 1.0

    async def _rows(
        self,
        query: str,
        qvec: str,
        groups: Sequence[str],
        model: str | None,
        filters: SearchFilters,
        *,
        nv: int,
        nk: int,
        n: int,
        rrf_k: int | None = None,
    ) -> Sequence[Any]:
        idents, wv, wk = self.fusion_params(query)
        params = {
            "qvec": qvec, "q": query, "ident": " ".join(idents) or None, "wv": wv, "wk": wk,
            "groups": sorted(set(groups)), "model": model or "",
            "sources": filters.sources, "departments": filters.departments,
            "nv": nv, "nk": nk, "rrf_k": rrf_k if rrf_k is not None else self._s.rrf_k, "n": n,
        }  # fmt: skip
        async with self._sessions() as s, s.begin():
            # SET LOCAL equivalents (bind parameters are not allowed in SET)
            await s.execute(
                text("SELECT set_config('hnsw.ef_search', :v, true)"),
                {"v": str(self._s.hnsw_ef_search)},
            )
            if self._iterative_scan:
                await s.execute(
                    text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
                )
            return (await s.execute(text(_SQL), params)).mappings().all()

    async def candidates(self, query: str, groups: Sequence[str], depth: int) -> list[Candidate]:
        """Both branches' top-`depth` lists in one call (same SQL, same filters), for offline
        fusion experiments: every fusion setting is then evaluated without new DB/LLM calls."""
        if not groups:
            return []
        qvec = _vec_literal(await self._embedder.embed_query(query))
        rows = await self._rows(query, qvec, groups, self._embedder.model, SearchFilters(),
                                nv=depth, nk=depth, n=2 * depth)  # fmt: skip
        out = []
        for r in rows:
            if not set(groups).intersection(r["allowed_groups"]):
                raise RuntimeError("permission filter violated - refusing to return results")
            out.append(
                Candidate(
                    r["chunk_id"], r["document_id"], r["source_uri"], r["rank_v"], r["rank_k"]
                )
            )
        return out

    async def search(
        self,
        query: str,
        groups: Sequence[str],
        *,
        k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: SearchFilters | None = None,
        candidates_multiplier: int = 3,
    ) -> SearchResult:
        """`groups` MUST come from a verified token. No groups -> no results (default deny)."""
        t0 = time.perf_counter()
        timings: dict[str, int] = {}
        if not groups:
            return SearchResult([], self._embedder.model, {"total": 0})
        filters = filters or SearchFilters()
        use_vec = mode in (SearchMode.HYBRID, SearchMode.VECTOR)
        use_kw = mode in (SearchMode.HYBRID, SearchMode.KEYWORD)
        qvec = "[" + ",".join(["0"] * EMBEDDING_DIM) + "]"
        degraded: str | None = None
        if use_vec:
            try:
                qvec = _vec_literal(await self._embedder.embed_query(query))
            except (LLMError, EmbeddingError) as exc:
                if mode is SearchMode.VECTOR:
                    raise UpstreamUnavailableError(f"Vector search unavailable: {exc}") from exc
                # Hybrid degrades to keyword-only and SAYS so, instead of failing the request.
                use_vec, degraded = False, f"vector search unavailable ({type(exc).__name__})"
        timings["embed"] = int((time.perf_counter() - t0) * 1000)
        model = self._embedder.model

        t1 = time.perf_counter()
        rows = await self._rows(
            query, qvec, groups, model, filters,
            nv=self._s.vector_candidates if use_vec and model else 0,
            nk=self._s.keyword_candidates if use_kw else 0,
            n=k * candidates_multiplier,
        )  # fmt: skip
        timings["sql"] = int((time.perf_counter() - t1) * 1000)

        caller = set(groups)
        hits: list[Hit] = []
        per_doc: dict[UUID, int] = {}
        for r in rows:
            # Defence in depth: the SQL already filtered; this assertion catches a regression
            # in the query itself (and is what the leakage test exercises end to end).
            if not caller.intersection(r["allowed_groups"]):
                raise RuntimeError("permission filter violated - refusing to return results")
            if per_doc.get(r["document_id"], 0) >= self._s.max_chunks_per_document:
                continue
            per_doc[r["document_id"]] = per_doc.get(r["document_id"], 0) + 1
            hits.append(
                Hit(
                    chunk_id=r["chunk_id"],
                    document_id=r["document_id"],
                    title=r["title"],
                    source=r["source"],
                    source_uri=r["source_uri"],
                    sensitivity=r["sensitivity"],
                    chunk_index=r["chunk_index"],
                    content=r["content"],
                    rrf_score=float(r["rrf"]),
                    vector_rank=r["rank_v"],
                    keyword_rank=r["rank_k"],
                    similarity=float(r["sim"]) if r["sim"] is not None else None,
                    allowed_groups=list(r["allowed_groups"]),
                    metadata=r["metadata"] or {},
                )
            )
            if len(hits) >= k:
                break
        timings["total"] = int((time.perf_counter() - t0) * 1000)
        return SearchResult(hits, model, timings, degraded)


async def detect_iterative_scan(sessions: async_sessionmaker[AsyncSession]) -> bool:
    """pgvector >= 0.8 can keep scanning HNSW until enough rows pass the filter."""
    async with sessions() as s:
        version = await s.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    if not version:
        return False
    major, minor = (int(x) for x in str(version).split(".")[:2])
    return (major, minor) >= (0, 8)
