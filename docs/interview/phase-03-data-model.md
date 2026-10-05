# Interview prep — Phase 3: data model

## Q1. "Walk me through your data model."
**30-second answer:** One Postgres, one schema per owning service, and each service's DB role can write only its own schema. Incidents, evidence and hypotheses form an evidence graph: every hypothesis links to evidence with a SUPPORTS or CONTRADICTS edge. Safety rules are database constraints too. For example, a consequential tool call without an approval cannot even be recorded. And every index has a written reason that a test checks.

**2-minute answer:**
- Context: about ten services, with AI agents writing data I can't fully trust.
- Decision: schema-per-service with Postgres roles, and no foreign keys across schemas, so I can later move a schema to its own database without breaking joins.
- Core tables:
  - `incidents` plus an `incident_events` timeline.
  - `evidence` plus `hypotheses` plus `hypothesis_evidence` (the evidence graph).
  - `approvals`: single-use, expiring, bound to a SHA-256 of the exact action arguments.
  - `tool_calls` and `model_usage`, which give me the cost and trace KPIs.
  - `rag.documents` and `rag.document_chunks`, which carry `allowed_groups` for permission-aware retrieval.
- What breaks: a single shared Postgres is one failure domain. The fix at scale is database-per-service, which the no-cross-FK rule makes possible.

**Architecture in the repo:** `libs/db/src/aeoi_db/models/*.py`, `libs/db/alembic/versions/0001…0012`, `docs/data-model.md`.

**Tradeoff I chose on purpose:** one shared Postgres (a weaker failure boundary) instead of one database per service (more RAM, harder local dev). The ownership rules are already enforced, so splitting later is mechanical.

**Common mistakes:** a "shared" database where every service reads every table; enums as Postgres ENUM types; FKs everywhere "for integrity" that block any later split.

**Follow-ups:**
- "How does service A read B's data?" Through B's API or events, never its tables.
- "Why not one database per service now?" RAM on a laptop, and ADR-011 already pays a lot of distributed cost.
- "How do you report across schemas?" Through a read-only analytics replica or CDC into a warehouse, not by joining live tables.

## Q2. "How do you make sure users only retrieve documents they're allowed to see?"
**30-second answer:** The permission filter is part of the same SQL statement as the vector and keyword ranking: `allowed_groups && :user_groups`. The LLM never sees an unauthorised chunk, so it can't leak one. Each chunk's groups are copied from its document by a database trigger, so application code can't forge or forget them. Revoking access on a document revokes it on every chunk in the same transaction.

**Follow-ups:**
- "What about HNSW plus filters returning fewer than k results?" pgvector ≥0.8 iterative scans, over-fetching, or partitioning by sensitivity tier.
- "Per-user ACLs?" Groups scale; per-user sharing needs a ReBAC service (Zanzibar-style).

## Q3. "How do you know every index is justified?"
**30-second answer:** Each index has a `COMMENT ON INDEX` that names the query it serves, and an integration test fails if any index lacks one. A second test fails if a foreign key has no supporting index. So "explain every index" is enforced by CI, not left to a code reviewer's memory.

**Follow-up — "Name one non-obvious index":** `ix_outbox_unpublished` is a *partial* index (`WHERE published_at IS NULL`). It contains only the unpublished backlog, never the millions of published rows, so relay polling stays fast forever. Another one: BRIN on `log_events.ts`, a few KB for millions of append-only rows.

## Q4. "How do you test migrations?"
**30-second answer:** Three ways:
1. `alembic check` in CI: the models and the migrations must produce the same schema.
2. A full round trip, base to head and back to base, **with data loaded**.
3. Rule tests against the real catalog: index comments, FK indexes, no cross-schema FKs.

The data round trip found a real bug: the RBAC downgrade failed once users had roles.

## Q5. "Why synthetic data? Isn't it fake?"
**30-second answer:** It's fictional on purpose, and deterministic. Same seed, same fingerprint, byte for byte. I planted a known root cause with a decoy and a competing hypothesis, so I can measure whether the agents find the right answer. Real production data would have no ground-truth labels and would be a privacy problem.

**Honest weakness to admit before they find it:** the v1 historical text is template-based, so similarity search is too easy. I listed it as a known limitation, and I fix it before I report any retrieval metric (Phase 13).
