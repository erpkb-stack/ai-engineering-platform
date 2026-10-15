# ADR-033: One non-root image for the Python workspace; addresses from config; limits from measurement

Status: Proposed (verified on the Mac 2026-10-14: limits measured, restarts 0, OOM 0)
Date: 2026-10-14
Refines: ADR-011 (eleven services), ADR-009 (Kubernetes), ADR-014 (LLM routing), architecture.md §21

## Context
No service needs a container today: all run on the host with `make run-*`. Phase 22 (kind) does
need images. Measured on the owner's Mac (2026-10-14): Docker Desktop has 12 CPUs and 7.75 GiB;
the infrastructure limits already take 2.94 GiB (1.25 GiB in use). The 8 Python services share
the same libs and nearly the same dependencies: the runtime venv (no dev tools) is 192 MB,
measured in the sandbox; the dev venv is 301 MB. The build sandbox cannot pull any base image
(Docker Hub, GHCR, ECR, Debian mirrors blocked), so images are built first on the Mac.

## Decision
- **One image `aeoi-py` for all Python services**, multi-stage. Builder: `uv sync --frozen
  --no-dev --all-packages` from the committed lock (uv 0.11.32 from PyPI, `UV_PYTHON=python3.12`
  so a 3.13 base fails at once: tree-sitter 0.23.x has no 3.13 wheel), then `compileall` of the
  workspace's own code. Runtime: venv + code only, owned by root, `USER 10001`, no uv, no compiler,
  `HEALTHCHECK` = readiness on `$AEOI_SERVICE_PORT` (one definition for every service). Each
  compose service picks its uvicorn command. Kubernetes can still roll each Deployment on its own.
- **One image `aeoi-catalog`** (Maven build stage -> JRE 21, non-root, curl for its HEALTHCHECK).
  Its entrypoint reads `CATALOG_DB_PASSWORD_FILE` into the java process env only.
- **Build contexts are allow-lists** (`<name>.Dockerfile.dockerignore`, BuildKit): `**` first,
  then only what the image needs, then `secrets`, `.env*`, `data` excluded again. A root
  `.dockerignore` is the safety net for any other build.
- **Every address comes from config.** Compose sets every `*_url` setting of every service to a
  compose service name and that service's port, plus `AEOI_PG_HOST/PORT` (read by `aeoi_db` via
  `os.environ`, not a Settings field). `tests/unit/test_compose_contract.py` derives the list from
  the Settings classes and the code, not from the compose file.
- **The egress allow-list** in compose is exactly the hosts of the gateway's HTTP-backed tools
  (`rag:8004`, `service-catalog:8010`), tested both ways. With the Java switch off,
  `service-catalog:8010` stays listed: nothing calls it and the name does not resolve.
- **Token issuer is unchanged.** Keycloak's `--hostname` fixes `iss` to
  `http://localhost:8090/realms/aeoi`; containers fetch the KEYS from `keycloak:8080` (`AEOI_OIDC_JWKS_URL`).
- **Least-privilege secrets.** One compose secret per FILE; each container mounts exactly the files
  its settings read (tested both ways), read-only at `/app/secrets/<file>`. Compose secrets outside
  swarm are plain bind mounts - we say so instead of calling it a vault. The file's own mode decides
  whether uid 10001 can read it: `make apps-secrets-perms` sets the MOUNTED files to 644 and keeps
  `secrets/` at 700 (no other host user can enter it), an explicit owner step that `apps-up` checks.
  No secret value in any env var; the Anthropic key is a file only (env is visible in `docker inspect`).
- **The native Ollama** is reached by `AEOI_LLM_PROVIDER_OLLAMA_BASE_URL=http://host.docker.internal:11434/v1`.
  The loader accepts an env override ONLY for a local provider (`hosted: false`) and ONLY to a
  loopback / host-gateway name; a hosted provider's URL changes in the reviewed YAML only.
  `extra_hosts` is exactly `host.docker.internal:host-gateway` (tested: one more line could map the
  name to a remote IP and send unredacted prompts off the machine).
- **Hardening on every app container:** read-only root FS (+ small `/tmp` tmpfs), `cap_drop: ALL`,
  `no-new-privileges`, ports on 127.0.0.1 only, `pull_policy: never`, `NO_PROXY` for every
  internal name (Docker Desktop copies host proxy settings into containers; these calls carry user
  tokens). Forbidden and tested: `user`, `env_file`, `privileged`, `network_mode`, `pid`, `ipc`,
  `cap_add`, `devices`.
- **Kafka healthcheck:** its probe starts a second JVM, which also INHERITED the broker's 512m heap.
  Now: own `-Xmx64m` heap, fast-start JIT, every 2 s while starting, then every 60 s (it caused
  peaks to the 1.5-core cap every 10 s; median ~2 %, measured on the Mac with our services on or off).
  After the change: median 3.4 %, max 121 % - fewer and lower spikes, not gone (a TCP-only probe
  would remove them but proves only "port open", not "broker answers").
- **Profiles follow `.env` as the containers see it:** `apps-up` reads them from `docker compose
  config` (make and compose parse `.env` differently: quotes, `True`/`1`).
- **Memory limits come from measurement.** Measured with `AEOI_APP_MEM_LIMIT=2g` (so no limit cut
  a peak), `make apps-peaks` over 15 min while multi-smoke (a full investigation), smoke,
  tools-smoke and approval-smoke ran (Mac, 2026-10-14, 184 samples):

  | container | peak MiB | limit |
  |---|---|---|
  | agents | 81 | 256M |
  | api | 100 | 256M |
  | audit | 105 | 256M |
  | incident-service | 95 | 256M |
  | llm-gateway | 96 | 256M |
  | orchestrator | 125 | 256M |
  | rag | 93 | 256M |
  | tool-gateway | 97 | 256M |
  | service-catalog (JVM, -Xmx256m) | 173 | 512M (heap + non-heap) |
  | infra: kafka / keycloak / postgres / redis | 619 / 584 / 216 / 22 | 1G / 768M / 1G / 192M |

  Rule: about 2x the highest Python peak, one value for all (a per-service number would be false
  precision from one run). Total app limits 2.5 GiB of the ~4.8 GiB left; all containers peaked
  at 2406 MiB of 7.75 GiB. The Phase 1 guess (150-250 MB per Python service) was 1.5-3x too high.
- **Images (Mac):** `aeoi-py` 460 MB, `aeoi-catalog` 468 MB (`docker image ls`).

## Alternatives
| Option | Why not (here) |
|---|---|
| One image per service | 8 nearly identical builds; nothing new tested; revisit when dependency sets diverge |
| Build images in Phase 22 only | two kinds of failure (image vs cluster) in one phase |
| Images for evaluation / knowledge-service | empty stubs; deferred to the phase that gives them work |
| Migrations as a one-shot compose service | the host commands work; Phase 22 needs a Job anyway |
| Mount the whole `secrets/` folder | every container could read every password and the signing keys |
| Ollama URL by a second routing YAML | two policy files drift; the env override is narrower and tested |

## Tradeoffs
- Every service carries every other service's dependencies (the 192 MB venv). Accepted.
- Any code change re-runs the dependency layer (all source is copied before `uv sync`); the uv
  cache mount keeps it fast. Splitting manifests from source is a later optimisation.
- The JWKS realm (`aeoi`) is written in compose, not derived from the issuer (tested to match).
- `routing.local.yaml` users need an empty `secrets/anthropic_api_key.txt` (compose cannot mount
  an optional secret).
- Images are verified on the Mac only (sandbox registry block); arm64 (CI matrix) is Phase 24.

## Prototype vs Production vs Enterprise-scale
[P] one image, compose, files as secrets, loopback ports. [Prod] per-service images when they
diverge, pinned base digests, SBOM + signing, secrets manager, migration Job. [Ent] a golden base
image owned by a platform team; admission policies (non-root, signed, pinned digests).

## When we would revisit
A service needs a heavy dependency the others do not (e.g. a local embedding model in-process), or
image size or build time starts to slow Phase 22 rollouts.
