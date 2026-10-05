# audit (Python 3.12 / FastAPI) — local port 8008

**Purpose:** Append-only audit trail for every agent step, tool call, approval and data access; query API for the Security/Audit UI.

**Owns schema:** `audit` (audit_events, append-only)
**Events:** consumes ALL domain events (audit topic)
**Must never:** Allow UPDATE/DELETE. Lose events (consumer uses DLQ + replay).
**Why this is a separate service:** Independent failure domain and retention policy from operational data.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
