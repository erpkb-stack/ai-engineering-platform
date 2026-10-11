"""Phase 13 (ADR-023): similar past incidents over rag.historical_incidents.

Hybrid like the document retriever (ADR-016): a keyword branch (Postgres FTS, OR-query) and a
vector branch (pgvector HNSW, cosine), fused by reciprocal rank (RRF). A third, tiny list
rewards the same service ("same service" is evidence of relevance, not of the same cause).
The group ACL is IN the SQL of both branches; groups come from the verified token only.

Each hit says whether every engineer may read it (`open_to_engineering` = the incident is
shared with `eng-all`): the historical-incident agent stores only those on the incident it
investigates, so a past security-team incident never leaks through a new incident's page.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_db.base import EMBEDDING_DIM
from aeoi_rag.embedder import Embedder

OPEN_GROUP = "eng-all"
RRF_K = 60
SERVICE_WEIGHT = 0.5  # one RRF "rank 1" vote at half weight: a tie-breaker, not a filter
DEPTH = 50  # candidates per branch before fusion
# HNSW returns at most ef_search rows (default 40 < DEPTH) and is approximate. The corpus has
# ~90 near-identical write-ups per category, so a small ef_search returned an ARBITRARY subset
# of them and RRF then rewarded whichever twins it happened to return (Mac smoke: no incident
# of the demo's own service in the top 5). 600 rows: a wide ef_search is near-exact and cheap.
EF_SEARCH = 400


def incident_text(row: Any) -> str:
    """What is embedded: what a past incident WAS and how it was fixed."""
    return f"{row.title}. {row.summary} Root cause: {row.root_cause} Fix: {row.remediation}"


async def embed_pending(
    sessions: async_sessionmaker[AsyncSession], embedder: Embedder, *, reembed: bool = False,
    batch: int = 32,
) -> tuple[int, str | None]:  # fmt: skip
    """Embed historical incidents that have no vector; `reembed` clears all vectors first
    (after an embedding-model change). Resumable: each batch commits."""
    if reembed:
        async with sessions() as s, s.begin():
            await s.execute(
                text("UPDATE rag.historical_incidents SET embedding = NULL, embedding_model = NULL")
            )
    done = 0
    while True:
        async with sessions() as s, s.begin():
            rows = (
                await s.execute(
                    text(
                        "SELECT id, title, summary, root_cause, remediation FROM "
                        "rag.historical_incidents WHERE embedding IS NULL ORDER BY id LIMIT :n"
                    ),
                    {"n": batch},
                )
            ).all()
            if not rows:
                return done, embedder.model
            vectors = await embedder.embed_documents([incident_text(r) for r in rows])
            for r, v in zip(rows, vectors, strict=True):
                await s.execute(
                    text(
                        "UPDATE rag.historical_incidents SET embedding = CAST(:v AS vector), "
                        "embedding_model = :m WHERE id = :id"
                    ),
                    {"v": _vec(v), "m": embedder.model, "id": r.id},
                )
            done += len(rows)


def _vec(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


_SQL = """
WITH q AS (
  SELECT CASE WHEN plainto_tsquery('english', :q)::text = '' THEN NULL
              ELSE replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery END AS tsq
),
kw AS (
  SELECT h.id, row_number() OVER (ORDER BY ts_rank_cd(h.tsv, q.tsq) DESC, h.id) AS rk
  FROM rag.historical_incidents h, q
  WHERE :nk > 0 AND q.tsq IS NOT NULL AND h.tsv @@ q.tsq
    AND h.allowed_groups && CAST(:groups AS varchar[])
    AND (CAST(:exclude AS text) IS NULL OR h.incident_key <> CAST(:exclude AS text))
    AND (CAST(:only AS text) IS NULL OR CAST(:only AS text) = ANY(h.service_keys))
    AND (NOT :shared_only OR CAST(:open AS text) = ANY(h.allowed_groups))
  ORDER BY ts_rank_cd(h.tsv, q.tsq) DESC, h.id
  LIMIT :nk
),
vec AS (
  SELECT id, row_number() OVER (ORDER BY dist, id) AS rv FROM (
    SELECT h.id, h.embedding <=> CAST(:qvec AS vector) AS dist
    FROM rag.historical_incidents h
    WHERE :nv > 0 AND h.embedding IS NOT NULL AND h.embedding_model = :model
      AND h.allowed_groups && CAST(:groups AS varchar[])
      AND (CAST(:exclude AS text) IS NULL OR h.incident_key <> CAST(:exclude AS text))
      AND (CAST(:only AS text) IS NULL OR CAST(:only AS text) = ANY(h.service_keys))
      AND (NOT :shared_only OR CAST(:open AS text) = ANY(h.allowed_groups))
    ORDER BY h.embedding <=> CAST(:qvec AS vector)
    LIMIT :nv
  ) v
)
SELECT h.incident_key, h.title, h.summary, h.root_cause, h.root_cause_category, h.remediation,
       h.service_keys, h.severity, h.occurred_at, h.resolved_at, h.allowed_groups,
       kw.rk AS rank_k, vec.rv AS rank_v,
       (CAST(:open AS text) = ANY(h.allowed_groups)) AS open_to_engineering,
       (SELECT count(*) FROM vec) AS vec_hits,
       COALESCE(1.0 / (:rrf_k + vec.rv), 0) + COALESCE(1.0 / (:rrf_k + kw.rk), 0)
       + CASE WHEN CAST(:svc AS text) IS NOT NULL AND CAST(:svc AS text) = ANY(h.service_keys)
              THEN :sw / (:rrf_k + 1) ELSE 0 END AS score
FROM rag.historical_incidents h
LEFT JOIN kw ON kw.id = h.id
LEFT JOIN vec ON vec.id = h.id
WHERE (kw.id IS NOT NULL OR vec.id IS NOT NULL)
ORDER BY score DESC, h.occurred_at DESC, h.incident_key
LIMIT :k
"""


async def search(
    sessions: async_sessionmaker[AsyncSession],
    query: str,
    groups: Sequence[str],
    *,
    k: int,
    qvec: Sequence[float] | None,
    model: str | None,
    service_key: str | None = None,
    only_service: str | None = None,
    shared_only: bool = False,
    use_keyword: bool = True,
    iterative_scan: bool = False,
    ef_search: int = EF_SEARCH,
    exclude: str | None = None,
    service_weight: float = SERVICE_WEIGHT,
) -> list[dict[str, Any]]:
    """`groups` MUST come from a verified token. No groups -> nothing (default deny).
    `qvec=None` = keyword only (the vector branch is off, e.g. the embedder is down).
    `service_key` BOOSTS that service; `only_service` FILTERS to it (Phase 7 behaviour).
    `shared_only`: only incidents shared with every engineer - what an agent may store on
    another incident (ADR-023); restricted ones are then never even read."""
    if not groups:
        return []
    params = {
        "q": query, "groups": sorted(set(groups)), "exclude": exclude, "open": OPEN_GROUP,
        "qvec": _vec(qvec if qvec is not None else [0.0] * EMBEDDING_DIM),
        "only": only_service, "shared_only": shared_only, "model": model or "", "nv": DEPTH if qvec is not None and model else 0,
        "nk": DEPTH if use_keyword else 0, "svc": service_key, "sw": service_weight,
        "rrf_k": RRF_K, "k": k,
    }  # fmt: skip
    async with sessions() as s, s.begin():
        # SET LOCAL equivalents (bind parameters are not allowed in SET), as in search.py
        await s.execute(
            text("SELECT set_config('hnsw.ef_search', :v, true)"), {"v": str(ef_search)}
        )
        if iterative_scan:  # pgvector >= 0.8: keep scanning until enough rows pass the filters
            await s.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
        rows = (await s.execute(text(_SQL), params)).mappings().all()
    caller = set(groups)
    out = []
    for r in rows:
        if not caller.intersection(r["allowed_groups"]):  # defence in depth, as in search.py
            raise RuntimeError("permission filter violated - refusing to return results")
        out.append({**r, "score": float(r["score"])})
    return out
