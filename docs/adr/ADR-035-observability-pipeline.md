# ADR-035: One OTLP pipeline in compose; an investigation is one trace, also after a resume

Status: Proposed (23a verified on the Mac 2026-10-20; 23b open)
Date: 2026-10-17
Refines: ADR-010 (OpenTelemetry), architecture.md §19, ADR-034 (apps on kind)

## Context
ADR-010 chose OpenTelemetry; nothing is instrumented yet. One investigation crosses 8 Python
services, a background run, Kafka and the Java catalog. Found in the code: a RESUMED
investigation runs without a correlation id (nothing stores it), and Kafka carries the
correlation id but no trace context. Measured on the owner's Mac (2026-10-17, idle, empty):
collector + Tempo + Prometheus + Loki + Grafana = ~263 MiB, ~4 % CPU (architecture.md guessed
~1 GB). Owner decisions: the full §19 scope, apps on kind.

## Decision
- **Collector and backends in compose** (profile `observability`), like the data services: one
  OTLP/HTTP address `otel-collector:4318` for kind pods and compose apps alike. The 22a generator
  adds one egress rule per app (compose subnet, 4318) and nothing else.
- **SDK in `libs/observability`**: traces, metrics, logs over OTLP/HTTP; FastAPI, httpx and
  psycopg 3 instrumentation; Kafka propagated by hand (our outbox and consumer); health routes
  excluded; a root CLIENT span is dropped (background pings are not traces); OFF unless
  `AEOI_OTEL_ENDPOINT` is set. stdout JSON logs stay and gain `trace_id`.
- **The public edge does not trust an incoming traceparent** (the api's /api/ routes): a client
  cannot join another investigation's trace or switch it off; internal calls continue it.
- **The investigation owns its trace context**: the W3C `traceparent` and the correlation id are
  stored at creation; every run (first or resumed) starts from them.
- **Kafka**: the outbox row stores the `traceparent` at write time; the consumer continues it.
- **Logs via OTLP from the SDK**, not a node agent (a hostPath DaemonSet would break the
  namespace's Pod Security "restricted").
- **LLM cost stays in Postgres** (`model_usage`, exact): Grafana reads it with a read-only login;
  it is not copied into Prometheus.
- **No content in telemetry**: no header, body or query capture; an exception keeps its TYPE only
  (messages hold DB rows: psycopg's "Failing row contains ...") - in the SDK and again in the
  collector, which also deletes header/query attributes and blocks token-shaped values; the
  catalog's Java agent does not export logs. A unit test plants secrets and asserts none reaches
  any exporter; the Mac smoke scans the whole exported trace and logs for every token.
- **Dashboards are provisioned files written by code** (`scripts/obs_dashboards.py`), each
  answering one named question; a test checks every datasource, metric and column they use.
  Grafana reads Postgres through column grants only; a free-text column a dashboard needs is
  exposed as a DERIVED column (23b: `tool_calls.denial_code`, never `denial_reason`).

## Alternatives
| Option | Why not (here) |
|---|---|
| Collector in the cluster (gateway) | the [Prod] pattern; here a second address for compose apps and another generated object, no new guarantee |
| Node log agent (filelog DaemonSet) | needs hostPath = Pod Security "privileged" for its namespace |
| LLM cost as Prometheus metrics | a second, approximate copy of exact rows we already have |
| Langfuse/LangSmith | LLM-only view; misses HTTP, Kafka, DB (ADR-010) |
| A new trace per resumed run (span links only) | the operator would need to know a resume happened to find the second trace |

## Tradeoffs
- Memory: the guess was ~0.3-0.6 GiB - wrong. Measured UNDER LOAD (23c, three smokes running):
  881 MiB for the 5 containers (Tempo 227, Grafana 252, Loki 176, collector 131, Prometheus 95);
  limits set to ~2x those peaks.
- The OTel Java agent fattens the catalog's latency tail: p99 780.6 ms with it vs 135.7 ms
  without (p50 unchanged; one run of 300 each, 23c). Kept on: its traces found the tail.
- Trace size: one investigation = 615-631 spans, 660-662 with a resume - mostly psycopg
  checkpoint writes. Far below Tempo's 5 MB per trace, but it grows with the graph.
- 100 % sampling: fine at dev traffic, wrong at production scale (tail sampling then).
- A long investigation is one long trace (minutes); Tempo handles it, some UIs page it.
- Collector down = telemetry dropped, never requests failed (by design, tested).

## Prototype vs Production vs Enterprise-scale
[P] compose collector + Tempo/Prometheus/Loki/Grafana, 24 h retention. [Prod] in-cluster collector
gateway, managed backends (Grafana Cloud / AMP + AMG), tail sampling. [Ent] per-tenant pipelines,
cost controls, SLOs with error-budget alerts.

## When we would revisit
Replicas (22b) need the collector per node; a second environment needs managed backends.

## Verified on the Mac (2026-10-17..20) - what held, what broke
Held: one trace per investigation across 8 services, also after the orchestrator pod was deleted
mid-run (2 `investigation.run` spans, one trace, 3 runs); logs in Loki carry the trace id and ONE
correlation id; no token in the exported data (9 tokens scanned); the collector stopped =
`make smoke` 12/12; the Java agent exports traces and metrics.
Broke, and taught something (7 fixes, phase-23.md section 7):
- 3 in the readiness wait (no `chown` in the Tempo image; a dropped connection while starting;
  "ready" is text, not JSON): the sandbox ran the binaries, never the images or the wait itself.
- The switch was off in `.env` and the smoke noticed only after a 73 s run - now a 1 s refusal.
- Tempo's search drops a trace id's leading zeros (~1 run in 16); the v1 tag API returns [] for
  a scoped tag. Both reproduced against the real Tempo binary before the fix.
- The trace exposed an older gap: the LLM client never sent the correlation id (10 extra ids per
  investigation). The smoke had checked "ours is present", not "only ours".
- The logs exposed an older bug: a dropped upstream connection was a 500 at the api, not a 503.
Lesson: an e2e test of the DATA path is not a test of the tooling around it; and a check that
asks "is X present" passes when X is drowned in noise.
