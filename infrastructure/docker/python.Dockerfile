# ONE image for every Python service (Phase 21, ADR-033). Each compose service picks its command:
#   uvicorn --factory aeoi_<name>.main:build_app --host 0.0.0.0 --port <port>
# Build (from the repo root):  make images
# Its ignore file is python.Dockerfile.dockerignore (next to this file): secrets/, .env*, data/,
# tests and the Java service never enter the build context.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------- builder: resolve + install
FROM ${PYTHON_IMAGE} AS builder
# the uv that wrote uv.lock (lock revision 3); from PyPI, so no extra registry is needed
ARG UV_VERSION=0.11.32
# UV_PYTHON: fail at once if the base is not 3.12 - tree-sitter 0.23.x (pinned for the Intel Mac,
# ADR-024) has no 3.13 wheel and would try to compile (found in the sandbox, Phase 21)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs/ libs/
COPY scripts/synth/ scripts/synth/
COPY services/ services/
COPY infrastructure/docker/healthcheck.py infrastructure/docker/healthcheck.py
# Phase 22a (ADR-034): the initContainer that waits for READY dependencies (compose depends_on)
COPY infrastructure/docker/wait_for.py infrastructure/docker/wait_for.py
# --frozen: fail if uv.lock does not match the pyprojects (never re-resolve inside a build)
# --no-dev: no pytest/ruff/mypy in the image; --all-packages: every workspace service
# editable (the default) on purpose: config code finds its files relative to the SOURCE tree
# (SERVICE_DIR = Path(__file__).parents[2]); the paths are the same /app in both stages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages
# the workspace's OWN code is not byte-compiled by uv (only site-packages are): do it here, or
# every start compiles it again in memory (the file system is read-only at run time)
RUN python -m compileall -q libs services scripts/synth infrastructure/docker

# ---------------------------------------------------------------- runtime: no uv, no compiler
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 aeoi \
 && useradd --system --uid 10001 --gid aeoi --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin aeoi
WORKDIR /app
# owned by root and not writable by the service user: code and venv are read-only at run time.
# WORKDIR matters: settings find secrets/ and data/ from the CURRENT directory (find_repo_root)
COPY --from=builder /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
# readiness of whichever service runs here (compose sets AEOI_SERVICE_PORT); Kubernetes
# (Phase 22) uses its own probes on the same /health/ready
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
  CMD ["python", "/app/infrastructure/docker/healthcheck.py"]
# no default service: a container must say which one it is (compose `command`)
CMD ["python", "-c", "import sys; sys.exit('aeoi-py: set the service command (see docker-compose.yml)')"]
