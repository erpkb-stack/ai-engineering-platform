# incident-service (Python 3.12 / FastAPI) — port 8001 — package `aeoi_incident`

**Purpose:** system of record for incidents, timeline, evidence, approvals and feedback.
**Owns schema:** `incident` (connects as LOGIN user `incident_svc` ∈ `svc_incident`; `make db-users`).
**Events (outbox → relay → publisher):** IncidentCreated, IncidentUpdated, InvestigationRequested (topic `incident.lifecycle`).
**Must never:** call LLMs, execute remediation, read another schema.
**Why a separate service:** owns the transactional incident lifecycle; must stay up when every AI service is down.

## Code map
- `api.py` routes `/v1/...` — re-check permissions (`require(Perm.X)`) even behind the gateway
- `service.py` use cases (caller owns the transaction) · `domain.py` pure rules (transitions, cursors, If-Match)
- `idempotency.py` DB-backed Idempotency-Key (same tx as the result; race → replay winner)
- `events.py` `stage_event()` + `OutboxRelay` + `LogPublisher` / `KafkaPublisher` / `InMemoryPublisher`

## Rules
- Every state change = row change + `incident_events` row + outbox event, in ONE transaction.
- POST that creates something requires `Idempotency-Key`. PATCH requires `If-Match: "<version>"`.
- Status changes only through `domain.TRANSITIONS`.

Run: `make run-incident` · Tests: `services/incident-service/tests` (unit), `tests/integration/services` (DB).
