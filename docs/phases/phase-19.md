# Phase 19 - Redis: deferred to Phase 22 (owner decision 2026-10-14, ADR-031)

## Gate
Phase 18 verified on the Mac and merged (PR #20 for 18a, PR #21 for ADR-030; main ac011b4).

## Challenge (read in the code, not assumed)
Rate limits, the LLM cache, idempotency and "one investigation per incident" all work today,
correct for one replica. Idempotency and the lock belong in Postgres and stay there. Redis is
needed only when a service has more than one replica - first in Phase 22.

## Result
No code. ADR-031; the Redis work (shared token bucket with Lua, shared LLM cache, Redis-down
tests, a 2-replica test) is now part of Phase 22's roadmap row.
