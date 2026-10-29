# ADR-044: Alerts open incidents through a signed webhook; triage is a rule table in code

Status: Proposed (29b built 2026-10-27; sandbox only)
Date: 2026-10-27
Refines: ADR-043 (the demo starts from an alert), docs/demo/demo-scenario.md minute 1:00

## Context
- The demo script starts with "an alert fires and opens an incident". Only `POST /incidents`
  existed: a person typing a form. An alert sender (Prometheus Alertmanager) has no user and
  no JWT.
- Every other public route needs a bearer token. A route without one is the easiest thing in
  the system to abuse: anyone who can reach it could open incidents (pages, noise, cost).
- Alertmanager re-sends a firing alert every `repeat_interval` (default 4 h), retries on any
  non-2xx, and runs in HA pairs that both send. "Twice" is the normal case, not an edge case.

## Decision
- `POST /api/v1/alerts` takes the **Alertmanager webhook payload (v4)** as is (unknown fields
  ignored, read fields bounded: max 20 alerts, 64 labels, 2000-char values, 64 KB body).
- **Authentication = HMAC-SHA256** with a shared secret (`secrets/alert_webhook_secret.txt`,
  `make alert-secret`) over `"<unix ts>." + the exact body bytes`, in `X-AEOI-Signature:
  sha256=<hex>` + `X-AEOI-Timestamp`. Refused (401) when missing, wrong, or more than 300 s
  off. Compared in constant time. No secret file = 503 "not configured", never accept-all.
- Checked **twice**: at the api edge (rate limit per sender address -> size -> signature,
  BEFORE forwarding: unsigned requests never reach a service) and again in incident-service
  (the policy point; a direct call cannot skip it). The api forwards the RAW bytes. A unit test
  fails if any other `/api/v1` route is reachable without a bearer.
- **Dedupe** = the alert's own identity: one incident per (fingerprint, startsAt), stored with
  the incident in one transaction (the Idempotency-Key table, 30-day TTL). A re-send, an HA
  twin or a replay inside the 300 s window returns the first result (`duplicate`). A new
  `startsAt` (it fired again later) is a new incident.
- **Triage is code** (`aeoi_incident.alerts`), written into the IncidentCreated event (source
  ALERT, actor `service:alerts`): severity from the label (critical SEV1, error/high SEV2,
  warning SEV3, info SEV4, unknown SEV3 and said so); a catalog **tier-1** service is raised
  one level (max SEV1). No model decides severity.
- **affected_services = [the alert's service] only.** The catalog's dependents are written
  into the description as the blast radius. Found while building: the orchestrator
  investigates the FIRST 3 affected services, so putting checkout-api's 6 dependents there
  would send the agents to victims, not the cause.
- **The catalog is optional and never loses an alert:** incident-service's own service token
  (`catalog:read`, `make tools-tokens`), one 2 s call per service per batch, none after it
  failed once in a batch (20 x 2 s must not outlast the api's 5 s upstream timeout). Down or
  off -> the label severity, and the rule says "tier unknown (<why>)".
- Per-alert outcomes (`created`, `duplicate`, `ignored_resolved`, `rejected`) with **200**:
  a 4xx/5xx for one bad alert would make Alertmanager re-send the whole group. A `resolved`
  alert never closes an incident (a human decides). An alert without a valid service label is
  `rejected` (an incident with no service cannot be investigated).

## Tradeoffs
- A shared secret, not mTLS or per-sender keys: one sender exists. Rotation = a new file and
  a restart of api + incident-service (no dual-secret window yet). [Prod] per-sender secrets
  in a secrets manager with an overlap window, or mTLS from the alerting system.
- The secret is in two services (edge + policy point). Both already mount secrets read-only;
  the extra copy buys "unauthorised requests never reach a service" without trusting the edge.
- The rate limit is per sender address: behind one proxy every sender shares a bucket. An
  unsigned flood spends that bucket and can delay real alerts from the same address.
- No correlation: a second, different alert on the same service opens a second incident. A
  human merges. Correlating alerts is a product decision, not built.
- Alert text is untrusted (like a typed title): stored as data; it reaches a model only via
  the existing wrapping and the LLM gateway's PII/secret scrubbing.

## Amendment 2026-10-29 (Phase 29 gate, fix5): the time budget is a total, and "ready" is warm
Measured, not assumed (Tempo traces of the 4 slowest alert requests on the Mac; 3 of 7 gate
runs FAILED with `webhook answered 503: incident-service timed out.`):
- The "2 s" catalog call was httpx `timeout=2.0`, which limits EACH step, not the call:
  incident-service waited 2.2 / 2.7 / 3.7 / 4.0 s. With 1.3-1.5 s lost before the handler
  started (a CPU-starved node) and 0.3-0.7 s of DB work, it answered after the api's 5 s.
- The api then said 503 for incidents that WERE created (INC-10332, 10334, 10336 exist).
- A fresh catalog answered its first request in 2.1 / 3.5 / 17.8 / 21.1 s while its readiness
  probe was UP: each failed run's incident was SEV2 "tier unknown" instead of SEV1.

Decisions (owner chose all four):
1. The catalog lookup has a TOTAL deadline of 1.5 s (`asyncio.timeout`; note
   `catalog too slow (no answer in 1.5 s)`). `tests/unit/test_alert_budget.py` keeps
   measured start delay + deadline + DB work + 1 s margin <= the api's upstream timeout.
2. The catalog warms its real path before Spring publishes ACCEPTING_TRAFFIC
   (`CatalogWarmup`: SQL + JSON writer, and a self-call through the filter chain and the RSA
   verify with a well-formed token whose signature is zero - always 401, tested), and builds
   the DispatcherServlet at start. A warm-up error never stops the start.
3. A timed-out non-GET at the api says the request may have been applied and to retry the
   SAME request. Status stays 503 (Alertmanager retries 5xx); 504 would be more exact - not
   changed here.
4. The demo's `alert_intake` requires SEV1 by the tier-1 rule (the script, minute 1:00): the
   degraded SEV2 keeps the alert, but it is not the demo and it hid the cold catalog.

Tradeoffs: a slower start (the warm-up runs before ready; bounded by a 10 s HTTP timeout). A
1.5 s deadline gives SEV2 "tier unknown" more often on a slow catalog - the alert is never lost
and the reason is written. Not fixed: the 1.3-1.5 s lost on a starved node before any handler
runs (the machine: a 60 W charger, CPU_Speed_Limit 31-58). [Prod]: per-route deadline
propagation (the caller's remaining budget in a header), and the first-request latency after a
rollout as a measured SLO.
