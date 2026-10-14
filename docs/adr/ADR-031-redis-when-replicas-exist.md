# ADR-031: Redis is added when a service has more than one replica (Phase 22), not before

Status: Proposed
Date: 2026-10-14
Refines: architecture.md §18 (Redis), roadmap Phase 19 and Phase 22

## Context
The roadmap planned Phase 19 as "Redis: caches, rate limits, locks, idempotency store,
Redis-down tests". Owner decision (2026-10-14), after reading what the code does today:
defer it.

Every planned Redis use already has a working implementation, correct for ONE replica:
| Planned use | Today | Wrong only when |
|---|---|---|
| Rate limits (api, tool-gateway) | in-memory token bucket, `aeoi_common.ratelimit` | more than 1 replica: each has its own bucket, the limit multiplies |
| LLM response cache | in-process LRU + TTL 1 h, `aeoi_llm/cache.py` | more than 1 replica; a restart empties it |
| Idempotency keys | Postgres, committed WITH the result (`aeoi_incident/idempotency.py`) | never: Redis alone cannot be atomic with the write |
| "One investigation per incident" | enforced in the database (409 "already RUNNING") | never |

Every service runs as one process on the owner's Mac, and Phase 21 (Docker) keeps one container
per service. The first time a service has more than one replica is Phase 22 (Kubernetes, HPA).

## Decision
No Redis code in Phase 19. Phase 22 takes over, as part of "more than one replica":
- shared token bucket (Redis + a Lua script, atomic) behind the existing `RateLimiter`
  interface, for api and tool-gateway;
- the LLM response cache in Redis, same key;
- Redis-down behaviour, as architecture.md states it: rate limiting fails OPEN for reads and
  CLOSED for consequential actions, caches are bypassed - each with a test;
- a test that runs 2 replicas and shows the limit is shared (the reason Redis is there).
Idempotency stays in Postgres; locks stay database constraints. Redis stays in compose (already
running, 64 MB) so nothing else changes.

## Alternatives
| Option | Why not (now) |
|---|---|
| Build it all in Phase 19 | code for a problem that does not exist yet; its only proof (2 replicas sharing a limit) needs Phase 22 anyway |
| Only the Redis rate limiter, as portfolio material | the same: without replicas it cannot be shown working |
| Redis for idempotency | Postgres commits the key with the result atomically; Redis would be a cache in front, no gain |

## Tradeoffs
- An LLM cache entry is lost when `make run-llm` restarts (TTL is 1 h anyway): small, accepted.
- Phase 22 gets bigger. Accepted: that is where the work can be proven.

## Prototype vs Production vs Enterprise-scale
[P] in-process limiters and cache, one replica each. [Prod] (from Phase 22) Redis/ElastiCache
for limits and caches, Postgres for idempotency. [Ent] a rate-limit service or the API gateway
does the limiting at the edge.

## When we would revisit before Phase 22
Any service needs a second process locally (e.g. two orchestrator workers), or LLM spend
measured in `llm.usage` shows repeated identical calls lost to restarts.
