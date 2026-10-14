# audit (Python 3.12 / FastAPI) — local port 8008 — package `aeoi_audit`

**Purpose:** append-only audit trail + scoped query API (ADR-017).
**Owns schema:** `audit` (audit_events: partitioned monthly, INSERT/SELECT only, trigger blocks UPDATE/DELETE)
**Write:** `POST /v1/events` — service tokens with scope `audit:write`; batches ≤ 500; idempotent on
(id, occurred_at); the receiver stamps `details._ingested_by`. [P] producer = tool-gateway relay.
**Consume (Phase 18a, ADR-029):** `consumer.py` - `incident.lifecycle` → one row per event (id = event_id, idempotent),
`_ingested_by: kafka:<topic>`; invalid envelope = poison → DLQ; DB down = wait. On only with
`AEOI_AUDIT_CONSUMER_ENABLED=true` (+ Kafka); `/health/ready` is 503 unless the consumer is running AND
the broker answers a probe. A time > 1 day in the future = poison. Dedupe key = (event_id, occurred_at).
**Read:** `GET /v1/events` — user tokens; scope decided from perms (all | own; read_incident = own
until incident assignment exists, Phase 16);
window ≤ 31 days; keyset pagination. `audit:read_team` → 403 until team data exists (Phase 26).
**Must never:** update or delete; let a request parameter widen its scope; let a user write.

Commands: `make run-audit` · `make stop-audit`. Tests: `tests/integration/tools/test_tools.py`.
