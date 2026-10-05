# incident-service (Python 3.12 / FastAPI) — local port 8001

**Purpose:** System of record for incidents, incident_events, timeline, approvals, feedback.

**Owns schema:** `incident`
**Events:** publishes IncidentCreated, HumanReviewRequired, HumanApproved, HumanRejected, IncidentResolved; consumes HypothesisCreated, ValidationCompleted
**Must never:** Call LLMs. Execute remediation.
**Why this is a separate service:** Owns the transactional incident lifecycle; must stay up even if all AI services are down.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
