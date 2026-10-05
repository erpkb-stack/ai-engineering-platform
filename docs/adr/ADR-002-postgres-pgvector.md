# ADR-002: PostgreSQL + pgvector for relational, full-text and vector data

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
We need relational data (incidents, approvals), full-text search, vectors, and permission filters, ideally in one query with transactional consistency.

## Decision
PostgreSQL 16 with pgvector (HNSW, cosine) and built-in FTS (tsvector/GIN). Schema per service.

## Alternatives
| Option | Why not (here) |
|---|---|
| Pinecone/Weaviate/Qdrant | Second datastore; permission filter and joins split across systems; sync lag; cost. |
| Elasticsearch/OpenSearch | Great BM25 + kNN, but heavy on a laptop and another source of truth. |
| Separate DB per service | Purer microservices; too much RAM locally. Schema-per-service keeps ownership boundaries. |

## Tradeoffs
Against: filtered HNSW can return < k results; one Postgres is a shared failure domain; vector scale ceiling roughly 10⁷ per node. Mitigation: iterative scans/over-fetch, read replicas, partitioning, documented migration to a dedicated vector store.

## Consequences
Simple ops, ACID with vectors, one backup story. Must watch memory for HNSW indexes.

## Prototype vs Production vs Enterprise-scale
[P] one container. [Prod] RDS/Aurora Multi-AZ + read replica + PgBouncer. [Ent] shard by tenant/sensitivity or move vectors to a dedicated engine.

## When we would revisit
Corpus > ~10M chunks, p95 ANN > 200 ms after tuning, or filter recall issues not solved by iterative scan.
