# ADR-013: One Alembic migration stream for all service-owned schemas (for now)

Status: Proposed
Date: 2026-10-05
Supersedes: —

## Context
Each schema has exactly one owning service (ADR-011, architecture.md §18). In a mature
microservice estate each service owns its migrations and deploys them independently. In
Phase 3 most of those services do not exist yet, but the data model must be designed,
migrated and tested as a whole now.

## Decision
- ORM models live in `libs/db/src/aeoi_db/models/<schema>.py`, one module per schema/owner.
- One Alembic environment (`libs/db/alembic`), one linear history, 4-digit revision ids,
  generated **per schema** (`make db-revision SCHEMA=incident ...`) so each revision is reviewable.
- Ownership is enforced in the database, not by convention: NOLOGIN role `svc_<service>`
  per schema with DML only on its own schema; no foreign keys across schemas (tested).
- `catalog` is excluded: the Java service owns it with Flyway (ADR-005, Phase 20).

## Alternatives
| Option | Why not (here) |
|---|---|
| Alembic env per service now | Needs 8 service skeletons before any service code exists; 8 version tables to coordinate locally. |
| Raw SQL migration files (Flyway/sqitch for Python too) | Loses `alembic check` (models vs DB drift detection), which caught real issues in this phase. |
| `Base.metadata.create_all()` | No history, no downgrade, no review. Fine for tests, never for a shared DB. |

## Tradeoffs
Strongest argument against: **one stream couples deploys**. A migration for `rag` and one
for `incident` are ordered in one history, so one bad migration blocks everyone's.
Mitigation: per-schema revisions, `alembic check` in CI, expand/contract only.

## Consequences
- Services import their models from `aeoi_db.models.<schema>`; they never import another schema's module for writes.
- Moving a schema to its own service-owned migrations later = copy its modules + start a new
  Alembic env from the current state (`alembic stamp`), then delete them here.

## Prototype vs Production vs Enterprise-scale
[P] one stream. [Prod] split per service once each service has its own deploy pipeline
(planned at Phase 30 review). [Ent] migrations run as a pre-deploy job per service, with
online schema-change tooling for big tables.

## When we would revisit
When two services need to deploy schema changes on different days, or when a migration
for one schema has blocked another team's release.
