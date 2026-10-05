# Phase 3 — Database & data model

## 0. Gate and scope challenge
- **ADRs are still `Proposed`.** This phase builds directly on ADR-002 (Postgres + pgvector) and ADR-006 (permission filter in SQL). If you disagree with either, say so now. Changing them after Phase 6 is expensive.
- **Scope decision: the whole relational model now, but only the seed data later phases need.**
  - Tables: all 9 schemas and 37 tables are designed and migrated now. A data model is reviewed as one whole, and interviewers ask about it as one whole.
  - Not built yet: chunks and embeddings. They need the embedding pipeline (Phase 6), so `document_chunks` is empty and `embedding` columns are NULL.
- **One change to the spec:** `services` and `repositories` are **not** Python tables. The spec lists them, but they belong to the Java Service Catalog (Phase 20, Flyway). Until then, `data/generated/catalog.json` holds the 150 services.
- **New ADR-013:** one Alembic migration stream for all schemas, for now. Running 8 per-service Alembic environments before the services exist is cost with no benefit.

## 1. Objective
Create a versioned, tested, least-privilege data model for every AEOI capability. Add a deterministic synthetic dataset with the demo incident planted inside it.

## 2. Business reason
Every KPI in architecture.md §4 is a query on these tables. MTTI comes from `incident_events`. Override rate comes from `approvals`. Cost per investigation comes from `model_usage`. Groundedness comes from `evaluations`. A weak data model makes those numbers impossible or wrong later.

## 3. Architecture
```
libs/db (aeoi-db)                               Postgres 16 + pgvector  (one instance [P])
  models/identity.py   ─┐                         schema      owner role         AI read-only?
  models/incident.py    │  SQLAlchemy 2.0          identity    svc_api            no
  models/orchestrator.py│  metadata                incident    svc_incident       yes
  models/rag.py         ├─► alembic (one stream) ─► orchestrator svc_orchestrator yes
  models/devdata.py     │   0001 schemas+roles     rag         svc_rag            yes
  models/tools.py       │   0002–0010 per schema   devdata     svc_tool_gateway   yes
  models/llm.py         │   0011 triggers,         tools       svc_tool_gateway   yes
  models/audit.py       │        partitions,       llm         svc_llm_gateway    yes
  models/eval.py       ─┘        index comments    audit       svc_audit (I+S)    no
                            0012 RBAC data          eval        svc_evaluation     yes
scripts/synth (aeoi-synth): seed=42 ─► generate() (pure) ─► COPY in 1 transaction ─► MANIFEST.json
```
Full reference: [`docs/data-model.md`](../data-model.md).

**What makes it more than CRUD tables:**
| Guarantee | Where it is enforced | Test |
|---|---|---|
| A service writes only its own schema | Postgres roles + default privileges | `test_roles.py` |
| No FK across ownership boundaries | model rule + catalog query | `test_model_rules.py`, `test_schema_rules.py` |
| Chunk ACL = document ACL (no forged groups) | BEFORE/AFTER triggers | `TestPermissionAwareRetrieval` |
| Engineers can't retrieve restricted text | `allowed_groups && :groups` in the same SQL | `test_engineer_query_never_returns_restricted_chunk` |
| Audit is append-only | role grants **and** trigger (blocks even the owner) | `TestAudit` |
| A consequential tool call can't be recorded without approval | CHECK constraint | `TestToolCalls` |
| Approvals: one pending per action, single-use, reason required, args hash | CHECK + partial unique index | `TestApprovals` |
| One RUNNING investigation per incident (duplicate delivery) | partial unique index | `TestOrchestrator` |
| Every index has a written reason | `COMMENT ON INDEX` + catalog query | `test_every_explicit_index_is_documented` |
| Every FK has a supporting index | catalog query | `test_every_foreign_key_has_a_supporting_index` |
| Models == migrations | `alembic check` | `test_models_match_migrations`, CI |
| Downgrade works with data in the tables | round trip after seeding | `test_round_trip_with_data_present` |

## 4. Files
```
libs/db/  pyproject.toml  alembic.ini  alembic/env.py  alembic/script.py.mako
          alembic/versions/0001 … 0012
          src/aeoi_db/{config.py, base.py, models/{identity,incident,orchestrator,rag,devdata,tools,llm,audit,eval}.py}
          tests/test_model_rules.py
scripts/synth/  pyproject.toml  src/aeoi_synth/{vocab.py, generate.py, load.py, __main__.py}  tests/test_generate.py
scripts/sql/verify-phase3.sql
tests/integration/conftest.py  tests/integration/db/{test_migrations,test_schema_rules,test_constraints,test_roles,test_seed}.py
docs/data-model.md  docs/adr/ADR-013-single-migration-stream.md  docs/phases/phase-03.md  docs/interview/phase-03-data-model.md
Makefile (db-* targets, .env export)  pyproject.toml (workspace, mypy, ruff)  .github/workflows/ci.yml  .claude/settings.json
.claude/rules/{database,testing}.md  .claude/skills/db-migration/SKILL.md  CLAUDE.md  AGENTS.md  libs/CLAUDE.md
architecture.md (§18 notes, ADR list)  docs/demo/demo-scenario.md (planted times)  docs/roadmap.md
```

## 5–7. Commands (macOS / zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull && git switch -c phase-3-data-model
uv sync --all-packages                     # new deps: sqlalchemy, alembic, psycopg, pgvector
make up                                    # postgres + redis
make db-upgrade                            # applies 0001…0012
make db-check                              # expect: "No new upgrade operations detected."
make db-seed                               # ~190k rows in a few seconds; prints counts + fingerprint
make db-verify                             # proof queries (below)
make check && make test-integration        # expect 60 unit passed; 37 integration passed
```
No new configuration. The DB URL comes from `secrets/postgres_password.txt` plus `AEOI_PG_PORT` in `.env` (the Makefile now exports `.env`). In CI and production, set `AEOI_DATABASE_URL` instead.

## 8–10. Tests and verification
**Already verified in the sandbox** (real Postgres 16 + pgvector 0.6 on Linux):
- 60 unit tests and 37 integration tests pass. `mypy --strict` and ruff are clean.
- `alembic check` reports no drift. Round trip base → head works, both empty and with data.
- Two schema-rule queries were mutation-tested: dropping an index comment and dropping an FK index were both detected.
- Seed: 150 services, 600 historical incidents, 1,100 documents, 1,784 deployments, 3,601 commits, 2,195 PRs, 39,262 logs, 151,460 metric points. Fingerprint `497466df4a89f163` (seed 42). Your Mac must print the **same fingerprint**; if it doesn't, determinism is broken.

**What `make db-verify` must show on your Mac:**
| Section | Expected |
|---|---|
| 3 deploy → commit | `DEPLOY-4821 · 2026.10.02.4 · 09:42` → "Batch order lookup…" |
| 4 signal order | app p95 jumps at **09:49**, pool = 1.00 at **09:50**, 5xx +42% at **09:51** |
| 5 first pool timeout | `09:50:10` |
| 6 similar incidents | INC-2911 among pool-exhaustion incidents for checkout-api |
| 7 restricted doc for eng-all | `0` |
| 8 undocumented indexes | no rows |

**Phase 3 is done when:** `make db-verify` matches the table above on your Mac, and the CI job `compose infra + database integration` passes on the PR.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `secrets/postgres_password.txt not found` | `make setup` not run | `make setup` |
| `password authentication failed` | the volume was created with an older password | `make clean CONFIRM=1 && make up && make db-upgrade && make db-seed` |
| `relation "incident.incident_number_seq" does not exist` while writing a new migration | autogenerate doesn't emit sequences | add `op.execute("CREATE SEQUENCE …")` (see 0003) |
| `NameError: pgvector` in a migration | autogenerate renders vector types without the import | add `import pgvector.sqlalchemy` |
| `A value is required for bind parameter` in raw SQL | `:word` in `op.execute` text is a bind parameter | avoid `:name` in literal text (this happened in 0011) |
| `db-reset` downgrade failed with FK error | reference data referenced by real data | fixed in 0012; covered by `test_round_trip_with_data_present` |
| Different fingerprint than `497466df4a89f163` | non-deterministic code crept into `aeoi_synth` | `test_deterministic_same_seed`; never use the clock, `set` order or Faker |

## 12. Production considerations
- **Connections:** each service connects as a LOGIN user that is a member of its `svc_*` role (Phase 4). PgBouncer in transaction mode sits in front at [Prod].
- **Big tables:** `log_events` and `metric_points` are simulations here. Real systems use Loki/Prometheus behind the Tool Gateway. If they stayed in Postgres: daily partitions plus retention by `DROP PARTITION`.
- **Audit:** run `SELECT audit.ensure_partitions(2)` monthly (K8s CronJob). Export to S3 with object lock for WORM.
- **HNSW:** the index builds empty now; the cost comes when chunks arrive (Phase 6). Bulk-load first, then build (or `SET maintenance_work_mem`). pgvector ≥0.8 iterative scans fix the filtered-ANN recall problem (ADR-002).
- **Zero-downtime:** only expand → migrate → contract. `CREATE INDEX CONCURRENTLY` on big tables (needs `autocommit_block()`).

## Known limitations (honest list)
1. **Historical incident text is template-based.** Titles repeat across incidents of the same category, so keyword "similar incident" search looks better than it really is. Phase 13 must add varied text (more templates or LLM paraphrase with a fixed seed). Otherwise the retrieval evals will be meaningless.
2. `historical_incidents` and `documents` have no embeddings until Phase 6.
3. Per-service LOGIN users don't exist yet. Services and tests connect as the owner `aeoi`. Role behaviour is tested with `SET ROLE`.

## 13–15. Interview prep
See [`../interview/phase-03-data-model.md`](../interview/phase-03-data-model.md).
