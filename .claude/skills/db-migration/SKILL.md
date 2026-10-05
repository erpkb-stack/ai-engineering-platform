---
name: db-migration
description: Create a safe Alembic migration for a service-owned schema, with index justifications and zero-downtime considerations. Use when adding/altering tables, columns, indexes or pgvector settings.
argument-hint: "<service> <message>"
---
# Migration: $ARGUMENTS

1. Change the model in `libs/db/src/aeoi_db/models/<schema>.py`, then `make db-revision MSG="<message>" SCHEMA=<schema>` — then READ and edit the generated file: autogenerate misses sequences, triggers, `COMMENT ON INDEX`, grants, and needs `import pgvector.sqlalchemy` for vector columns.
2. Zero-downtime pattern (expand → migrate → contract): add nullable column → backfill in batches → add constraint `NOT VALID` → `VALIDATE CONSTRAINT`. Indexes on big tables: `CREATE INDEX CONCURRENTLY` (needs `op.get_context().autocommit_block()`).
3. Every index gets a comment `-- serves: <query/endpoint>`.
4. Test: `make db-upgrade && make db-check && make test-integration` (the suite round-trips base↔head with data present).
5. Never edit a merged migration (hook-enforced).
