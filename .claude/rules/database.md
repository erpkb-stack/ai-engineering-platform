---
paths:
  - "**/alembic/**"
  - "**/models/**"
  - "**/*.sql"
  - "libs/models/**"
---
# Database rules

- PostgreSQL 16 + pgvector. One logical DB locally, **one schema per owning service** (`incident`, `rag`, `audit`, `eval`, `catalog`, …). A service never writes another service's schema.
- Alembic migrations are forward-only once merged; data migrations are separate revisions.
- Every new index needs a one-line comment explaining the query it serves (spec requirement).
- UUIDv7 primary keys (time-ordered → better B-tree locality than v4).
- `audit_events` is append-only: no UPDATE/DELETE grants for the app role.
- Vector columns: HNSW index with `vector_cosine_ops`; record embedding model + dimension on every row.
- Permission filters (`allowed_groups && :user_groups`) go in the SAME query as the vector search.
