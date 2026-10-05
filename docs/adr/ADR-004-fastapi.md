# ADR-004: FastAPI for Python services

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
AI services are IO-bound (LLM, DB, tools), need typed contracts and OpenAPI.

## Decision
FastAPI + Pydantic v2 + uvicorn, async SQLAlchemy, dependency injection via `Depends`.

## Alternatives
| Option | Why not (here) |
|---|---|
| Django/DRF | Batteries included but sync-first ORM; heavier. |
| Flask | Owner knows it; async and typing weaker. |
| gRPC | Better for internal high-throughput RPC; browser/OpenAPI story weaker. Possible [Ent] for internal calls. |

## Tradeoffs
Against: async misuse (blocking calls in the loop) silently kills throughput. Mitigation: lint rules, `anyio.to_thread` for blocking libraries, load tests.

## Consequences
OpenAPI drives the TS client generation.

## Prototype vs Production vs Enterprise-scale
[P]/[Prod] same. [Ent] consider gRPC between internal services.

## When we would revisit
If CPU-bound work dominates (move it to workers) or internal RPC latency matters.
