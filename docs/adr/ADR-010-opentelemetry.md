# ADR-010: OpenTelemetry for traces, metrics and logs

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
One investigation spans ~10 services, Kafka hops and dozens of LLM/tool calls. Debugging needs end-to-end correlation.

## Decision
OTel SDK in every service, context propagation over HTTP + Kafka headers, Collector → Prometheus/Tempo/Grafana; GenAI semantic conventions where available.

## Alternatives
| Option | Why not (here) |
|---|---|
| Vendor APM agent | Faster setup, lock-in. |
| LangSmith/Langfuse only | Great LLM-level view, but misses infra/Kafka/DB. Can be added as an extra exporter. |

## Tradeoffs
Against: instrumentation effort; GenAI conventions are still evolving. Mitigation: shared `libs/observability`.

## Consequences
Prompt/response content is stored in DB (scrubbed), not span attributes.

## Prototype vs Production vs Enterprise-scale
[P] Collector + Prometheus + Tempo + Grafana optional profile. [Prod] managed backends. [Ent] tail sampling, cost controls.

## When we would revisit
Never; exporters may change.
