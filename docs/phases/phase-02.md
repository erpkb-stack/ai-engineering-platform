# Phase 2 — Repository & development environment

## 0. Gate and scope challenge
- Phase 1 is marked done because you started Phase 2. **ADR-001…012 are still `Proposed`.** Accept or change them before Phase 3, because Phase 3 builds the data model on ADR-002 and ADR-006.
- **One scope cut (Kafka is now opt-in):** the roadmap had Kafka in the default `infra` profile. Nothing uses Kafka until Phase 18 (an in-process bus is used until then, per C12). Running it now costs about 0.8 GB of RAM on every `make up` for 16 phases. So `infra` = Postgres + Redis, and Kafka has its own `kafka` profile. CI still tests Kafka on every push, so it can't silently break.

## 1. Objective
Any machine (your Intel Mac or a clean CI runner) goes from `git clone` to green checks and healthy infrastructure with three commands: `make setup`, `make check`, `make up`.

## 2. Business reason
Every later phase depends on this one. A reproducible environment reduces "works on my machine" time, which is the same kind of waste AEOI targets for incidents. CI also gives a verified baseline before any AI code exists.

## 3. Architecture
```
Your Mac                                             GitHub Actions (every push / PR)
──────────────────────────────────────────           ─────────────────────────────────────
uv workspace (.venv, Python 3.12)                    quality:      ruff · mypy --strict · pytest+cov
  libs/common  libs/models                           secrets-scan: gitleaks (full history)
  libs/security libs/observability                   infra-smoke:  compose up (pg+redis+kafka)
pre-commit (ruff, mypy, gitleaks, large files,                     → verify-infra.sh → teardown
            uv.lock check, no commits to main)
docker compose  [infra]  postgres+pgvector, redis    (127.0.0.1 only)
                [kafka]  + kafka KRaft + topic job
```
**[P]** single-node everything, localhost-only ports.
**[Prod]** managed services: RDS, ElastiCache and MSK (ADR-002/003).
**[Ent]** the same contracts, plus multi-AZ, IAM auth and TLS everywhere.

## 4. Files
```
pyproject.toml  uv.lock  .python-version  .pre-commit-config.yaml  .gitleaks.toml  .env.example
Makefile (rewritten)  docker-compose.yml
infrastructure/docker/postgres/init/01-init.sql     # extensions + read-only MCP role
infrastructure/docker/kafka/create-topics.sh         # 8 topics incl. DLQs, idempotent
scripts/check-ports.sh  scripts/verify-infra.sh  scripts/doctor.sh (fixed earlier)
libs/common         uuid7, correlation id, problem+json errors, base settings
libs/models         EventEnvelope[T], Fact / Hypothesis / Recommendation + invariants
libs/security       wrap_untrusted(), redact_text(), redact_mapping()
libs/observability  structlog JSON logging with correlation id + redaction
libs/*/tests        37 unit tests
.github/workflows/ci.yml (new)  claude.yml (checkout bumped)
.claude/settings.json (make targets allowed/asked)  CLAUDE.md  docs/roadmap.md
```

## 5. Why the libs contain real code now
These four libraries define rules that every later phase depends on. If they are wrong, many services are wrong.
- **`Hypothesis` cannot be HIGH confidence** when there is contradicting evidence, or when it has fewer than 2 distinct pieces of supporting evidence. **A consequential `Recommendation` must require approval.** The schema enforces this, so no prompt can bypass it.
- **`EventEnvelope`** has the same shape on the in-process bus and on Kafka. That is why the Phase 18 swap is not a rewrite.
- **`wrap_untrusted()`** escapes `<`, `>` and `&`, so a document cannot close the wrapper and pose as instructions.
- **Redaction runs as the last log processor**, so nothing reaches stdout unredacted.

## 6. Commands (macOS / zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch -c phase-2-dev-env           # the hook blocks commits to main from now on
brew install uv                          # if `make doctor` showed it missing
make setup                               # .venv, pre-commit hooks, .env, secrets/postgres_password.txt
make check                               # expect: ruff OK, mypy "no issues", 37 passed
make up                                  # postgres + redis, waits until healthy
make verify-infra                        # expect: 8 passed, 0 failed
make up PROFILE=kafka                    # optional: + kafka and topics
make verify-infra                        # expect: 15 passed, 0 failed
make down
```
Commit and push:
```zsh
git add -A && git commit -m "Phase 2: uv workspace, shared libs, compose infra, CI"
git push -u origin phase-2-dev-env       # open a PR, watch CI go green, then merge
```

## 7. Configuration
| Item | Where | Secret? |
|---|---|---|
| Host ports, log level, URLs | `.env` (from `.env.example`) | no |
| Postgres password | `secrets/postgres_password.txt` (random, created by `make setup`) | **yes**: gitignored; Claude is denied read access |
| Read-only DB role `aeoi_readonly` | `01-init.sql` | no (local-only, cannot write; tested) |
| Kafka topics/partitions | `create-topics.sh` | no |

## 8. Tests
- **Unit (37):** uuid7 ordering, uniqueness and range; problem+json; settings from env with the DB URL hidden; envelope round trip; actor format; naive timestamp rejected; evidence id format and kind; every finding invariant; untrusted-wrapper break-out; attribute injection; truncation; 5 secret patterns; recursive key redaction; JSON log contents.
- **Infra smoke (`verify-infra.sh`):** pgvector version and cosine distance; pg_trgm; the read-only role is read-only and cannot write; Redis PING, SET/GET with TTL and eviction policy; all topics present, 6 partitions on `investigation.tasks`, Kafka produce → consume round trip.

## 9–10. Verified so far, and what you must verify
| Check | Where it ran | Result |
|---|---|---|
| `make setup` on a fresh clone, then `make check` | Linux sandbox | ✔ 37 passed, mypy strict clean, ruff clean |
| All pre-commit hooks except gitleaks | sandbox | ✔ (gitleaks was blocked: the sandbox cannot download Go) |
| `01-init.sql` on real Postgres 16 + pgvector, run twice | sandbox | ✔ idempotent; read-only role cannot write, even after `SET default_transaction_read_only=off` |
| `verify-infra.sh` logic against real Postgres + Redis | sandbox | ✔ 8/8, and a negative test caught 2 planted faults |
| `docker compose config`, actionlint on workflows | sandbox | ✔ |
| **Containers actually starting** (images, Kafka KRaft config, healthchecks) | **not yet**: Docker Hub is blocked in the sandbox | ⏳ **your Mac + CI must prove this** |

**Phase 2 is not done** until `make verify-infra` passes on your Mac with PROFILE=kafka, and the `infra-smoke` CI job is green.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `✘ port 5433 is used by: …` | another program uses the port | set `AEOI_PG_PORT=<free port>` in `.env` (and in `.mcp.json`). Defaults are already non-standard (5433/6380/9094) to avoid a local Postgres/Redis/Kafka |
| `up` times out on kafka | Docker Desktop RAM too low; the Kafka JVM was killed | Docker Desktop → Resources ≥ 6 GB; `make logs SVC=kafka` |
| Postgres starts but the password is wrong | the volume was created with an old password (init runs only once) | `make clean CONFIRM=1 && make up` |
| `pgvector missing` | the volume was created before init SQL existed | same as above |
| Commit blocked: "don't commit to branch" | you are on `main` | `git switch -c <branch>` |
| First commit slow (~1 min) | pre-commit downloads Go once to build gitleaks | one-time only |

## 12. Production considerations
- Postgres password file → **AWS Secrets Manager + IAM DB auth**. Redis → ElastiCache with AUTH and TLS. Kafka → MSK with RF=3, `min.insync.replicas=2`, IAM/SASL and TLS.
- `aeoi_readonly` for AI tooling is a real production pattern: give agents and MCP servers a **least-privilege, read-only, statement-timeout** role, and never the app owner role.
- Pin image digests in CI (Phase 21/24), not floating tags.
- The CI `infra-smoke` job becomes the base for integration tests (Testcontainers) in Phase 3+.

## 13–15. Interview prep
See [`../interview/phase-02-dev-environment.md`](../interview/phase-02-dev-environment.md).
