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
