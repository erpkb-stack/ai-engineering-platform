# ADR-043: The demo is a script of CHECKS on the product path; a pass counts only on real models

Status: Proposed (29a built 2026-10-27; sandbox rehearsal only)
Date: 2026-10-27
Refines: docs/demo/demo-scenario.md (Phase 1), roadmap Phase 29 gate

## Context
- Gate: "scripted demo passes 3 times in a row from a clean `make up`". The script (Phase 1)
  promised 4 things that do not exist: an alert webhook, an evidence-graph screen, a measured
  MTTI and a shown diff of removed sentences. Owner (2026-10-27): build all four (29b-29e).
- A demo judged by looking at a screen passes when it should not. Fake models never validate
  (ADR-037), so half the script (critic, validation, proposal, approval) cannot pass on them.
- Approving the rollback inserts a real deploy row on checkout-api: the next run would see a
  newer deploy than DEPLOY-4821 and be a different investigation.
- `make db-seed` TRUNCATEs rag documents, chunks and the embedded history: re-seeding before a
  demo would break the knowledge and history steps.

## Decision
- `make demo` runs the script through the product path as the seeded users and CHECKS each
  existing step in code (`tests/demo/demolib.py`, unit-tested): services ready, incident,
  investigation, the 4 evidence agents overlapping in time, evidence kinds, the top cause =
  DEPLOY-4821, the decoy never #1 or HIGH, critic + validation, the postmortem, the investigation's
  rollback proposal, ENGINEER 403 + IC approve -> EXECUTED + MITIGATED, the decision record
  (who, when, why, args hash, tool call), the injection document never returned.
- On fake models a real-model step is `n/a` and the verdict is REHEARSAL-OK - never a pass. On
  real models an `n/a` is a FAIL. Only real-model PASS runs count toward the gate.
- "Clean" (owner, 2026-10-27) = fresh containers and pods, data kept: `make demo-fresh` (down,
  up, k8s-apply, rollout restart, 180 s for the node to settle - 28a finding 2). No re-seed.
- `make demo-clean` after every pass removes ONLY the rollback-of-DEPLOY-4821 row; incidents,
  approvals and audit rows stay as the record of what the demo did.
- MTTI (29e) is quoted only with a measured manual baseline timed by the owner; never invented.

## Tradeoffs
- A checked demo is stricter than a live one: a correct but differently worded answer can fail a
  string check (DEPLOY-4821 in the statement). The checks use keys, not prose, where they can.
- Each counted pass costs real model calls (Phase 25: $0.98 for 16 investigations).
