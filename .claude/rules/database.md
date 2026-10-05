---
paths:
  - "**/alembic/**"
  - "**/models/**"
  - "**/*.sql"
  - "libs/models/**"
---
# Database rules

- PostgreSQL 16 + pgvector. One logical DB locally, **one schema per owning service** (`incident`, `rag`, `audit`, `eval`, `catalog`, …). A service never writes another service's schema.
- One Alembic stream for all owned schemas (ADR-013): `make db-revision MSG=… SCHEMA=<schema>`, 4-digit rev ids. Then READ the file: autogenerate misses sequences, triggers, comments, grants.
- `make db-check` must pass (models == migrations). Downgrades must work WITH data present (tested).
- Alembic migrations are forward-only once merged; data migrations are separate revisions.
- Every new index needs a `# serves: …` comment in the model AND a `COMMENT ON INDEX` in the migration (a test fails otherwise).
- No foreign keys across schemas (tested). Reference other services' data by id/key.
- Status/kind columns: `TEXT + CHECK`, not Postgres ENUM.
- UUIDv7 primary keys generated in the app (`aeoi_common.ids.uuid7`) — time-ordered, better B-tree locality than v4.
- `audit_events` is append-only: no UPDATE/DELETE grants for the app role.
- Vector columns: HNSW index with `vector_cosine_ops`; record embedding model + dimension on every row.
- Permission filters (`allowed_groups && :user_groups`) go in the SAME query as the vector search.
