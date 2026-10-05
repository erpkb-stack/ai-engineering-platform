# Standard Python service layout

```
services/<name>/
├── CLAUDE.md
├── Dockerfile               # multi-stage: builder (uv sync --frozen) → runtime (python:3.12-slim, UID 10001)
├── .dockerignore
├── pyproject.toml           # depends on libs/common, libs/models, libs/security, libs/observability
├── app/
│   ├── main.py              # create_app(): routers, middleware (correlation id, OTel, problem+json)
│   ├── config.py            # pydantic-settings, env prefix AEOI_<NAME>_
│   ├── deps.py              # DI: db session, current_user, clients
│   ├── api/v1/              # thin routers
│   ├── domain/              # business logic, pure where possible
│   ├── adapters/            # db repositories, kafka producer/consumer, http clients
│   └── events/              # Kafka handlers (idempotent)
├── alembic/                 # only if the service owns a schema
└── tests/                   # unit tests that live with the service
```
