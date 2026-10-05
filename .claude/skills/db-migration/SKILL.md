---
name: db-migration
description: Create a safe Alembic migration for a service-owned schema, with index justifications and zero-downtime considerations. Use when adding/altering tables, columns, indexes or pgvector settings.
argument-hint: "<service> <message>"
---
# Migration: $ARGUMENTS

1. `cd services/<service> && uv run alembic revision --autogenerate -m "<message>"` — then READ and edit the generated file; autogenerate misses pgvector types, partial indexes, and CHECK constraints.
2. Zero-downtime pattern (expand → migrate → contract): add nullable column → backfill in batches → add constraint `NOT VALID` → `VALIDATE CONSTRAINT`. Indexes on big tables: `CREATE INDEX CONCURRENTLY` (needs `op.get_context().autocommit_block()`).
3. Every index gets a comment `-- serves: <query/endpoint>`.
4. Test: `alembic upgrade head` then `alembic downgrade -1` then `upgrade head` on a Testcontainers DB.
5. Never edit a merged migration (hook-enforced).
