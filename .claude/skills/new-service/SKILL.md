---
name: new-service
description: Scaffold a new Python FastAPI microservice in services/ with the standard AEOI layout, health checks, OTel, Dockerfile, compose entry and nested CLAUDE.md. Use when adding or bootstrapping any Python service.
argument-hint: "<service-name> <port>"
---
# Scaffold service $ARGUMENTS

Follow `layout.md` in this skill folder exactly. Then:
1. Add the service to the root `pyproject.toml` uv workspace members.
2. Add a compose service under the correct profile with `mem_limit`, healthcheck, `depends_on: condition: service_healthy`.
3. Create `services/<name>/CLAUDE.md` (≤30 lines: purpose, owned schema, owned events, what it must never do).
4. Add `/health/live` and `/health/ready` (ready checks DB/Redis/Kafka it actually depends on — nothing else).
5. Add a smoke test `tests/integration/test_<name>_health.py`.
6. Before finishing, ask: "Could this be a module in an existing service instead?" and write the answer into the service CLAUDE.md "Why this is a separate service" line.
