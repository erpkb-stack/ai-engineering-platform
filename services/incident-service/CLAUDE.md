# incident-service (Python 3.12 / FastAPI) — port 8001 — package `aeoi_incident`

**Purpose:** system of record for incidents, timeline, evidence, approvals and feedback.
**Owns schema:** `incident` (connects as LOGIN user `incident_svc` ∈ `svc_incident`; `make db-users`).
**Calls (Phase 29b, optional):** service-catalog `GET /v1/services` with its own token (`secrets/incident_catalog_token.txt`, `catalog:read`) - alert triage only.
**Events (outbox → relay → publisher):** IncidentCreated, IncidentUpdated, InvestigationRequested, EvidenceRetrieved, HypothesisCreated, PostmortemGenerated, HumanReviewRequired, HumanApproved, HumanRejected, HumanMoreInfoRequested, ActionExecuted, ActionExpired (topic `incident.lifecycle`).
**Delegated tokens:** verified (`secrets/delegation_public.pem`) ONLY in X-On-Behalf-Of on the proposals route (bound to its investigation AND incident); consume refuses them.
**Must never:** call LLMs, execute remediation, read another schema.
**Why a separate service:** owns the transactional incident lifecycle; must stay up when every AI service is down.

## Code map
- `api.py` routes `/v1/...` — re-check permissions (`require(Perm.X)`) even behind the gateway
- `service.py` use cases (caller owns the transaction) · `domain.py` pure rules (transitions, cursors, If-Match)
- `idempotency.py` DB-backed Idempotency-Key (same tx as the result; race → replay winner)
- `postmortem.py` (Phase 12, ADR-022): timeline points (source derived from stored evidence kind, dedupe), postmortems, read-time assembly + per-reader redaction, markdown
- `approvals.py` (Phase 16, ADR-026): request (manual / investigation; one pending per args; proposal idempotent per investigation), decide (IC, not the requester; expired → EXPIRED + `ActionExpired`), consume (for the gateway, AS the approver: action + args hash + incident, once), outcome (→ MITIGATED; failed/rejected → back to INVESTIGATING if nothing else waits). Never executes anything.
- `alerts.py` + `catalog.py` (Phase 29b, ADR-044): `POST /v1/alerts` - NO JWT, HMAC signature (`aeoi_security.webhook`, re-checked after the api); triage = rule table in code (label severity, catalog tier 1 +1); affected = the alert's service ONLY (dependents = blast radius in the description); one incident per (fingerprint, startsAt) via the idempotency table (30 days); catalog optional, never loses an alert; actor `service:alerts`, event source ALERT
- `events.py` `stage_event()` + `OutboxRelay` + `LogPublisher` / `KafkaPublisher` / `InMemoryPublisher`

## Rules
- Every state change = row change + `incident_events` row + outbox event, in ONE transaction.
- POST that creates something requires `Idempotency-Key`. PATCH requires `If-Match: "<version>"`.
- Status changes only through `domain.TRANSITIONS`.

Run: `make run-incident` · Tests: `services/incident-service/tests` (unit), `tests/integration/services` (DB).
