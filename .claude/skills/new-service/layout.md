# Standard Python service layout

```
services/<name>/            # package name MUST be unique across the workspace: aeoi_<name>
├── CLAUDE.md
├── Dockerfile               # multi-stage: builder (uv sync --frozen) → runtime (python:3.12-slim, UID 10001)
├── .dockerignore
├── pyproject.toml           # depends on libs/common, libs/models, libs/security, libs/observability
├── src/aeoi_<name>/
│   ├── main.py              # build_app(settings) -> aeoi_web.create_app(...)  (uvicorn --factory)
│   ├── config.py            # pydantic-settings, env prefix AEOI_<NAME>_
│   ├── db.py                # async engine + session dependency (service LOGIN user, make db-users)
│   ├── api.py               # thin routers; every route Depends(require(Perm.X))
│   ├── domain.py            # pure rules (no IO), unit-tested
│   ├── service.py           # use cases inside a caller-owned transaction
│   └── events.py            # stage_event() into the outbox; relay; publishers
├── alembic/                 # only if the service owns a schema
└── tests/                   # unit tests; DB tests go to tests/integration/services/
```
