---
paths:
  - "services/**/*.py"
  - "libs/**/*.py"
  - "tests/**/*.py"
---
# Python conventions

- Python 3.12, `uv` workspace. Type hints everywhere; `mypy --strict` for `libs/`, `--strict` per service once it stabilises.
- Ruff for lint + format (line length 100). No `print` — use `libs/observability` structured logger.
- FastAPI: routers in `app/api/v1/`, dependencies in `app/deps.py`, business logic in `app/domain/`, IO in `app/adapters/`. Routes stay thin.
- Pydantic v2 models for every request/response and every Kafka event (`libs/models`). No bare dicts across a boundary.
- Async all the way: SQLAlchemy 2.x `AsyncSession`, `httpx.AsyncClient`, `aiokafka`. Never call blocking IO inside the event loop.
- Every outbound call has an explicit timeout, retry policy (tenacity, exp. backoff + jitter) and circuit breaker where the dependency is remote.
- Errors: raise domain exceptions; map to RFC 7807 problem+json in one exception handler.
- Propagate `correlation_id` and OTel context on every HTTP call and Kafka header.
