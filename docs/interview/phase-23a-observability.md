# Phase 23a - Observability pipeline: interview notes (ADR-035)

Measured facts only (Mac, 2026-10-17..20, kind cluster, 9 apps): one investigation = ONE trace
across 8 services, 615-631 spans (660-662 with a resume); the orchestrator pod deleted mid-run ->
2 `investigation.run` spans in the same trace (3 runs); 82-90 log lines from 7 services with that
trace id and one correlation id; no token in the exported data (9 tokens scanned); collector
stopped -> `make smoke` 12/12. The 5 backend containers peaked at 647 MiB after a smoke run
(not under load - the loaded peak is not measured yet).

## Questions
1. How does one investigation stay ONE trace when it runs in the background and survives a crash?
2. How do you keep secrets and personal data out of traces and logs?
3. Why is the collector in compose and not in the cluster?
4. Why is LLM cost in Postgres and not in Prometheus?
5. What did verifying on real infrastructure find that the sandbox did not?
6. What would you change for production?

## 30-second answers
1. The investigation row stores the W3C `traceparent` and the correlation id when it is created.
   Every run - the first one, or a resume after the pod restarts - starts from them
   (`continue_from`). Kafka events carry the traceparent written into the outbox row in the same
   transaction. The public edge ignores an incoming traceparent, so a client cannot join or
   pollute another investigation's trace.
2. No headers, bodies or query strings are captured. An exception keeps its TYPE only: Postgres
   messages contain table rows. The SDK removes it, and the collector removes it again, deletes
   header/query attributes and blocks token-shaped values. A unit test plants secrets and asserts
   none reaches any exporter; the Mac smoke scans the whole exported trace and its logs for every
   real token.
3. One OTLP address for kind pods and compose apps alike, like the databases. An in-cluster gateway
   is the production pattern, but here it would be a second address and one more generated object
   for no new guarantee.
4. `model_usage` already has the exact rows. A Prometheus copy would be approximate and could
   disagree. Grafana reads the rows with a read-only login that can see only listed columns
   (migration 0026; a test checks every dashboard query against that list).
5. Seven fixes. Three were in the "wait until ready" helper, which the sandbox never ran against
   the real images. Two were Tempo API details (leading zeros dropped from trace ids; the v1 tag
   API returns nothing for a scoped tag) - both reproduced against the real binary before fixing.
   Two were OLDER bugs the new telemetry made visible (next section).
6. Tail sampling in an in-cluster collector gateway, managed backends, per-tenant pipelines, SLOs
   with error budgets; 100 % sampling is only right at dev traffic.

## 2-minute answers
**Q5 (what telemetry found that tests had not).** The first full trace carried 11 correlation ids.
One Loki query grouped by service showed it exactly: the request's id everywhere, plus 10 ids on
the LLM gateway only - one per model call. The LLM client had never forwarded the header, since
Phase 5. My smoke had checked "our id is present", which passed; now it fails on ANY other id and
names the service. Second: a transient 500 in a smoke. The api's log line, found by its
correlation id, said `ReadError`: the upstream dropped the connection (a keep-alive to a pod that
had just been replaced). The proxy handled refused connections and timeouts, not this - so a
retryable condition became an opaque 500. Now it is a 503 that says "retry with the same
Idempotency-Key"; GETs retry once, POSTs never.

**Q1 (why not a new trace per resume, with span links?).** Then an operator would need to know a
resume happened to find the second half. Storing the context makes "one investigation, one
trace" true by construction, and the pod-kill test proves it.

**What I would say I did NOT prove.** Memory under load (only a post-run sample). The Grafana
"span -> logs" click (the same trace id in Loki is proven, the UI link was not checked by hand).
The Java catalog's slow tail (1.1-1.6 s server spans against a 2 s tool deadline; one call timed
out) - the Java agent is the first suspect, not proven. And a kind issue from 22a: after a Docker
restart the Mac could not reach the api until the node was recreated; my hypothesis (traffic
arrives from the other Docker network's gateway, which the policy does not allow) is unproven
because recreating the node destroyed the evidence - twice.
