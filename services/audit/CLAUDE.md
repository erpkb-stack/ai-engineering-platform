# audit (Python 3.12 / FastAPI) — local port 8008 — package `aeoi_audit`

**Purpose:** append-only audit trail + scoped query API (ADR-017).
**Owns schema:** `audit` (audit_events: partitioned monthly, INSERT/SELECT only, trigger blocks UPDATE/DELETE)
**Write:** `POST /v1/events` — service tokens with scope `audit:write`; batches ≤ 500; idempotent on
(id, occurred_at); the receiver stamps `details._ingested_by`. [P] producer = tool-gateway relay;
[Phase 18] a Kafka consumer of all domain events.
**Read:** `GET /v1/events` — user tokens; scope decided from perms (all | own; read_incident = own
until incident assignment exists, Phase 16);
window ≤ 31 days; keyset pagination. `audit:read_team` → 403 until team data exists (Phase 26).
**Must never:** update or delete; let a request parameter widen its scope; let a user write.

Commands: `make run-audit` · `make stop-audit`. Tests: `tests/integration/tools/test_tools.py`.
