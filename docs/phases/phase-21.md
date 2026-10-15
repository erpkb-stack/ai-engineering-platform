# Phase 21 - Docker (lean: one image, real addresses; ADR-033)

Status: VERIFIED on the Mac 2026-10-14 (section 10); fix1-fix3 applied.

## 0. Gate and scope challenge
- **Gate: passed.** Phase 20 verified on the Mac and merged (PR #23, main 2d8a9a0).
- **Challenge - the system does not need containers today.** Every service runs on the host
  and every smoke passes. The ONE real reason: Phase 22 (kind) cannot run code that has no image.
- **Owner decisions (2026-10-14):** ONE multi-stage non-root image for the Python workspace + one
  for the Java catalog; `evaluation` and `knowledge-service` (empty stubs) deferred.
- **Out of scope:** image push / Trivy / GHCR = Phase 24; migrations as a Job = Phase 22; Redis use
  = Phase 22 (ADR-031). Apps do not get Redis (nothing uses it): it is not in the `apps` profile.

## 0b. Measured on the Mac before design (2026-10-14)
| Fact | Value | Source |
|---|---|---|
| Docker Desktop | 12 CPUs, 7.75 GiB, Engine 29.8.1 | `docker info` |
| Infra in use / limits | 1.25 GiB / 2.94 GiB | `docker stats`, compose |
| Left for apps (vs limits) | about 4.8 GiB | 7.75 - 2.94 |
| JVM catalog | 197 MB resident at -Xmx256m | Phase 20 |
| Kafka CPU | median 1.9-3.5 %, peaks 160-178 %, our services on OR off | 3 x 20 samples |
- **Kafka peaks = its own healthcheck JVM** (`docker top`: 2 java processes), which also inherited
  the broker's 512m heap (review #5). Fixed: own small heap, slower interval after start.
- **My first memory command was wrong** (7 MB per service: `lsof | head -1` took a wrapper PID, and
  macOS compresses idle memory). Limits come from `docker stats` of the containers themselves.

## 1-2. Objective and reason
Every Python service and the catalog run as containers from `make apps-up`, and the EXISTING smokes
pass against them unchanged: the proof that the images are what Phase 22 can deploy. KPI: none
directly - an enabler, said honestly.

## 3. Architecture
```
host (Mac)                          Docker Desktop
  make smokes / browser ---------->  127.0.0.1:8000-8010 (same ports as make run-*)
  Ollama :11434 <------------------  llm-gateway (host.docker.internal, local-only override)
                                    profile apps: api incident-service orchestrator agents rag
                                                  llm-gateway tool-gateway audit (image aeoi-py)
                                    profile apps-catalog: service-catalog (aeoi-catalog)
                                    postgres (+ kafka when .env turns it on; keycloak-up)
  service -> service: http://<compose name>:<port>; postgres:5432; kafka:19092; keycloak:8080 (keys)
```
| Guarantee | Enforced by | Test |
|---|---|---|
| Non-root, no build tools at run time | multi-stage; `USER 10001`; runtime has no uv/pip | unit: Dockerfile stages; Mac: `id -u` = 10001 |
| Same code as the host | `uv sync --frozen --no-dev` from uv.lock | sandbox: frozen install OK (192 MB venv) |
| No secret in an image or a layer | allow-list ignore files; secrets mounted | unit: ignore order; Mac: file search in the image |
| Each container only its own secrets | one compose secret per file | unit: mounted == read by Settings, both ways |
| Every address from config | compose env for every `*_url` + `AEOI_PG_*` | unit: derived from Settings classes + code |
| Egress not wider | allow-list == HTTP tool hosts | unit: both ways |
| Ollama stays local | env override only for hosted:false + loopback names; exact extra_hosts | unit: 5 bad URLs + hosted refused |
| Hardened containers | read_only, cap_drop ALL, no-new-privileges, loopback ports, pull never | unit: + forbidden keys |
| Ready means ready | image HEALTHCHECK = /health/ready | sandbox: 8/8 ready; Mac: `docker compose ps` |

## 4. Files
```
infrastructure/docker/python.Dockerfile (+ .dockerignore)   ONE image, multi-stage, USER 10001
infrastructure/docker/catalog.Dockerfile (+ .dockerignore)  Maven -> JRE 21, USER 10001
infrastructure/docker/healthcheck.py                        readiness on AEOI_SERVICE_PORT, no proxy
.dockerignore                                               safety net (secrets/, .env*, data/)
docker-compose.yml        x-app-env / x-app anchors; 8 apps + service-catalog; 21 file secrets;
                          kafka healthcheck (own heap, start_interval); postgres in apps profiles
services/llm-gateway/src/aeoi_llm/config.py                 local-only base_url env override
scripts/phase21_apps.py   profiles / check / fix-perms / peaks
Makefile                  images, apps-up, apps-secrets-perms, apps-down, apps-stats, apps-peaks
tests/unit/test_compose_contract.py (72), services/llm-gateway/tests/test_config.py (+9), conftest.py
docs: ADR-033, roadmap, architecture.md §21, CLAUDE.md, infrastructure/CLAUDE.md, .env.example
```

## 5-7. Commands and configuration (macOS, zsh)
```zsh
make up                        # postgres (the apps profile also starts it)
make images                    # first build downloads the bases + Maven deps (several minutes)
make apps-secrets-perms        # ONCE: mounted secret files 644, secrets/ stays 700
make apps-up                   # precheck (images, secret files + modes, data/, ports), then up --wait
make smoke; make tools-smoke   # ... the existing smokes, unchanged
make apps-down                 # back to host runs
```
- `.env` switches apply to the containers too: `AEOI_TOOLS_CATALOG_URL` (adds `apps-catalog`),
  `AEOI_INCIDENT_EVENT_TRANSPORT=kafka` / `AEOI_AUDIT_CONSUMER_ENABLED=true` (adds kafka, started and
  topics created BEFORE the apps), `AEOI_OIDC_ISSUER` (keys from keycloak:8080), `LLM_ROUTING`.
- No key? `touch secrets/anthropic_api_key.txt` with `LLM_ROUTING=routing.local.yaml`.

## 8. Tests (sandbox)
- `make check`: 952 passed (lint + mypy + unit). Contract tests: 72; llm config: +9.
- Mutations: 24/24 caught (localhost URL, wrong port, missing URL, allow-list extra/missing, missing
  or extra secret, wrong target, read_only off, port on 0.0.0.0, password in env, other realm,
  host Kafka listener, ignore adds secrets back, no AEOI_PG_HOST, env_file, privileged, remote
  extra_hosts, NO_PROXY gap, orchestrator not waiting, short volume form, probe heap, re-include
  order, `!**`).
- **Container-like run** (no image possible here): the frozen no-dev install from the allow-listed
  context, code read-only, each service as uid 10001 with ONLY its compose secrets and env from
  docker-compose.yml (deviations: compose names -> 127.0.0.1, Postgres port 5433). 8/8 ready via the
  image's healthcheck script; `make smoke` 12/12, `make tools-smoke` 16/16. `make agent-smoke`
  13/1 - the SAME 13/1 with host-run services (baseline): fake models write no postmortem
  sentence (PARTIAL), not a container problem.
- Found here: Python 3.13 would break tree-sitter 0.23.x (no wheel) -> `UV_PYTHON=python3.12`.

## 9. Review (independent, before the Mac)
| Id | Finding | Fix |
|---|---|---|
| H1 | compose secrets outside swarm are bind mounts: 600 files unreadable for uid 10001 | `apps-secrets-perms` (explicit) + `apps-up` checks modes |
| M2 | `env_file`/`privileged`/`network_mode`/... would pass the tests | forbidden-keys test |
| M3 | `AEOI_PG_HOST/PORT` (os.environ) invisible to Settings-based tests | test scans the code |
| M4 | the 512M cap always on: "peaks" could never exceed it | `AEOI_APP_MEM_LIMIT` |
| M5 | Kafka probe JVM inherited the 512m heap | own heap + fast JIT |
| M6 | orchestrator resumed runs before api/incident were ready | depends_on healthy x3 |
| M7 | Kafka topics created after the apps started | kafka + kafka-init first |
| M8 | make vs compose read `.env` differently | profiles from `compose config` |
| L | pull of a missing local image; empty dir for missing data/; extra_hosts not exact; proxy env; ignore order; ignored AEOI_LOG_LEVEL; unused AEOI_REPO_ROOT; exit 2; duplicated catalog healthcheck/JVM opts; `apps-ps`; `# syntax` lines | all fixed or removed |
| open | JWKS realm written in compose (tested to match); empty Anthropic key file for local routing; layer cache split | recorded in ADR-033 |
- Found while testing the fixes: `apps-up` failed SILENTLY when Docker was not running -> explicit check.

## 9b. Mac findings
- `make check` failed once (`test_timeout_is_one_deadline_and_trips_the_breaker`, 2.7 s vs < 0.6 s)
  while the suite took 136 s; green on the re-run (952 passed, 75 s). The tool call itself ended at
  its deadline (`latency_ms=201`): the test times work OUTSIDE the deadline with the wall clock, so it
  fails on a loaded Mac. Follow-up (separate commit): assert the recorded latency instead. Also seen:
  a tool-gateway test logged `service=llm-gateway` (structlog context leaks between tests).
- fix1: `apps-up` printed "9 problem(s), nothing started" (host `make run-*` on every app port) and
  then STARTED Kafka and the apps anyway. Cause: macOS make 3.81 ignores `.SHELLFLAGS`, so the
  one-line recipe ran without `set -e`. Reproduced in the sandbox with `.SHELLFLAGS=-c`; the recipes
  now `set -eu -o pipefail` themselves (re-tested: stops, temp file removed).
- That run's smoke results came from the HOST services and do not count. One real number: the
  catalog CONTAINER peaked at 158 MiB (idle, just started). The risk-smoke `blast radius: timeout`
  happened while the host gateway's `localhost:8010` reached the starting catalog container next to
  the host catalog (macOS let both bind 8010).
- After fix1 on the Mac: `make apps-up` -> all 12 containers healthy; `make multi-smoke` in containers:
  investigation COMPLETE (9 tasks), 25/26. The 1 failure was a STALE check: it expected tool calls
  from exactly the 4 Phase 10 agents, but historical_incident (13) and code_intelligence (14) call
  tools too. fix2: the 4 must be there, nothing outside the 6 evidence agents may be (5 cases tested).
- Peaks during the light smokes (MiB): Python 76-118, catalog 159; all containers 2260 of 7936.
  The Phase 1 guess (150-250 MB per Python service) was 1.5-3x too high.
- fix2: risk-smoke `blast radius (service catalog): timeout` in containers. Logs: "Started" at
  23:11:24, but `HikariPool-1 - Starting...` only at 23:15:11 on a REQUEST thread, done 23:15:15
  (3.9 s) - the gateway's tool timeout is 2.0 s. The readiness probe never touched the DB, so
  "healthy" came before the pool existed (on the host it was fast enough: no CPU limit). Fix: the
  readiness group includes `db` (`CatalogReadinessTest`: db in readiness, NOT in liveness - a DB
  outage must not restart the JVM); the catalog gets 2 CPUs (startup was 39-47 s on 1). The 2 s
  tool timeout stays: a slow system of record must say "not ready", not make callers wait.
- Open, repo-wide (not Phase 21): 39 of 116 Makefile targets have lines that rely on `.SHELLFLAGS`
  (`-e`/`pipefail`) to stop on a failure; on make 3.81 they do not. Confirm with `make --version`,
  then fix in its own change.

## 10. Verify (gate for Phase 22) - on the Mac
- [x] `make check` green (952 passed; a first run failed one timing test under load - see 9b)
- [x] `make images` builds both images: `aeoi-py` 460 MB, `aeoi-catalog` 468 MB
- [x] `make catalog-build`: 23 Java tests (21 + 2 readiness), 0 failures/errors/skips (surefire reports)
- [x] `make apps-secrets-perms` (22 mounted files 644, secrets/ 700); `make apps-up` -> 12/12 healthy
- [x] smokes UNCHANGED against the containers: multi-smoke 26/26 (full investigation COMPLETE),
      smoke 12/12, tools-smoke 16/16, approval-smoke 23/23, risk-smoke 19/19 on a COLD catalog
- [x] `make apps-peaks` during the smokes -> limits (ADR-033 table): Python 256M, catalog 512M
- [x] `docker run --rm aeoi-py:dev id -u` = 10001; `/app` holds code only (no secrets/); the only
      key-like files in the image are PUBLIC CA certificates (/etc/ssl/certs, certifi) - no
      `*_svc_password*`, `*token.txt` or `.env*`
- [x] at the FINAL limits: kc-smoke 21/21 (Keycloak `iss` unchanged in containers), catalog-smoke
      6/6 (parity 0 mismatches / 150 services, JVM 109 MB), consumer-smoke 12/12, multi-smoke 26/26
- [x] after multi-smoke at the final limits: all 13 containers `restarts=0 oom=false`
- [x] Kafka CPU after the healthcheck change: median 3.4 %, max 121 % (was peaks 160-178 % every
      10 s; now every 60 s and a smaller probe JVM - fewer and lower spikes, NOT gone)
- [x] `make apps-down`; host runs still work: `make dev` + `make run-orch` + `make smoke` 12/12
      (`make smoke` needs the orchestrator since Phase 9: with `make dev` alone, investigate = 503,
      which is the correct answer - the Makefile hint said "make dev" only; fixed)

## 10b. Open items (not Phase 21; each gets its own change)
- make 3.81 ignores `.SHELLFLAGS`: 39 of 116 targets rely on `-e`/`pipefail` to stop on a failure.
- tool-gateway `test_timeout_is_one_deadline...` times work outside the deadline with the wall
  clock (failed once on a loaded Mac); structlog context leaks between tests (`service=llm-gateway`
  in a tool-gateway test).
- `apps-up`'s port precheck passed once while port 8001 was taken (Docker then refused the bind
  with a clear error). Cause unknown: the host service may have started after the check. Test:
  run `make dev`, wait, then `make apps-up` - it must refuse.
- Kafka probe: a TCP-only probe would remove the spikes but proves less ("port open" vs "broker
  answers"); kept the JVM probe.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| apps-up: "only YOU can read" | secret files 600 | `make apps-secrets-perms` |
| apps-up: "port 8006 ... outside this compose project" | a host `make run-tools` still running | `make stop-tools` |
| apps-up: "no local image" | images not built | `make images` |
| a container "unhealthy", logs PermissionError | a secret unreadable despite the check (Docker Desktop mapping) | send `docker compose logs <svc>` |
| llm calls fail, Ollama "connection refused" | Ollama not running, or not reachable from Docker | `ollama serve`; `docker compose exec llm-gateway python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags').status)"` |
| Keycloak token rejected in containers | Keycloak not running (keys come from keycloak:8080) | `make keycloak-up` |
| OOM-killed / restarting container | its real peak is above the limit | measure with `AEOI_APP_MEM_LIMIT=2g`, set the limit from the peak |
