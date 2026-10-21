@AGENTS.md

# CLAUDE.md — Claude Code project memory for AEOI

<!-- Keep this file under ~150 lines. Topic rules live in .claude/rules/ (path-scoped),
     procedures live in .claude/skills/, specialist reviewers in .claude/agents/.
     HTML comments like this one are stripped before Claude sees the file. -->

## Current phase
**Phase 24 — next (need check first)** (Phases 1–23c closed; 22b replicas + Redis DEFERRED by owner decision 2026-10-17 - keep every Deployment at 1 replica, see ADR-034; Phase 22a = the apps on kind from manifests generated from compose, default-deny policies, zero-downtime rollout, ADR-034; Phase 21 = one non-root image for the Python services + catalog image, measured limits, ADR-033; Phase 20 = lean contract-first Java catalog, ADR-032; Phase 18 = 18a only, agent tasks stay on HTTP per ADR-030; Phase 19 Redis deferred to Phase 22 per ADR-031; ADRs still `Proposed`, incl. ADR-014, ADR-016–ADR-035).
Phase status is tracked in `docs/roadmap.md`. Do NOT start a phase until the previous
phase's "Verify" checklist passes. Use the `/phase` skill to run a phase.

## How to work in this repo
- Before writing code for a phase, read the relevant section of `architecture.md` and any ADR it cites.
- Every phase follows the 15-step contract in `.claude/skills/phase/SKILL.md`.
- If a design decision changes, write/update an ADR with the `/adr` skill **in the same change**.
- Prefer the simplest design that meets the requirement. If the spec demands something
  heavier, implement it but record the cost in the ADR "Tradeoffs" section.
- Distinguish **Prototype / Production / Enterprise-scale** in every design note.
- When unsure whether something is a fact or a hypothesis — it is a hypothesis.

## Mentor mode (the owner asked for this explicitly)
Act as a ruthless Staff-level reviewer. Challenge unnecessary services, agents, frameworks,
weak data models, bad API design, poor observability and unjustified tech. Say "this is
unnecessary" when it is. Never flatter. Use the `architecture-critic` subagent for big reviews.

## Stack (pinned decisions — see ADRs)
- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x async + Alembic, `uv` for envs
- LangGraph for orchestration (ADR-001); PostgreSQL 16 + pgvector (ADR-002)
- Kafka (KRaft, single broker locally) (ADR-003); Redis 7
- Java 21 + Spring Boot 3 for `service-catalog` (ADR-005)
- React + TypeScript + Vite for `frontend/`
- LLMs: Anthropic Claude for reasoning agents; Ollama local model for cheap tasks / offline dev;
  local embedding model by default. Provider is configuration, never hard-coded (Feature 18).
- OpenTelemetry → Prometheus/Grafana (ADR-010); Docker Compose locally; kind for local K8s

## Machine constraints (owner's Mac)
- Intel x86_64 macOS. No Apple-Silicon GPU → Ollama runs CPU-only: use ≤3–4B models locally.
- Docker Desktop memory is the main bottleneck. Use compose **profiles**
  (`infra`, `core`, `agents`, `observability`, `full`) — never require all 11 services + infra
  just to run one test.

## Commands
- `make doctor` — prerequisites · `make setup` — one-time env, hooks, .env, secrets
- `make check` — lint + mypy --strict + unit tests (must pass before any commit)
- `make up [PROFILE=infra|kafka]` · `make verify-infra` · `make down` · `make logs SVC=x`
- DB: `make db-upgrade` · `make db-check` (drift) · `make db-revision MSG=… SCHEMA=…` · `make db-seed` · `make db-verify` · `make test-integration`
- Data model reference: `docs/data-model.md`. Models: `libs/db/src/aeoi_db/models/<schema>.py`.
- Services: `make db-users` · `make dev` (api :8000 + incident :8001) · `make smoke` (needs `make run-orch` too: investigate goes to the orchestrator since Phase 9) · `export TOKEN=$(make -s token ROLE=SRE)`
- LLM: `make run-llm [LLM_ROUTING=routing.local.yaml|routing.test.yaml]` (:8005) · `make stop-llm` · `make llm-smoke` · `make ollama-pull` · `make -s service-token SERVICE=x SCOPES=llm:invoke`
- LLM calls from code: only via `aeoi_llm_client.LLMClient` (tests: `FakeLLMClient`). Pick a ROUTE, never a model.
- RAG: `make rag-token` · `make rag-ingest` · `make rag-eval [RERANK=1]` · `make rag-sweep` · `make rag-bench` · `make run-rag` (:8004) · `make rag-smoke` · `make docpack`
- Retrieval changes are judged by `make rag-eval` diffs only (data/eval/results/), never by one query.
- Tools: `make tools-tokens` · `make run-tools` (:8006) · `make run-audit` (:8008) · `make tools-smoke` · stop with `make stop-tools` / `make stop-audit`
- Agents: `make agents-tokens` · `make run-agents` (:8003) · `make agent-run INCIDENT=INC-n [ROUTE=local|fast]` · `make agent-compare INCIDENT=INC-n` · `make agent-smoke`
- Orchestrator (Phase 9): `make delegation-keys` · `make run-orch` (:8002, resumes RUNNING investigations on start) · `make stop-orch` · `make orch-smoke` · `make agent-status INCIDENT=INC-n`. Product path: `POST /api/v1/incidents/{id}/investigate` → 202 + `/api/v1/investigations/{id}`.
- Delegation (ADR-019): a background run acts for the user ONLY via an investigation+incident-bound delegated token from the api's token exchange; delegated tokens are accepted ONLY in `X-On-Behalf-Of` (`Authenticator.verify_obo`), never as a bearer. Graph nodes must be idempotent (a kill re-runs the interrupted node).
- Multi-agent (Phase 10, ADR-020): `make investigate INCIDENT=INC-n [AGENTS=a,b]` · `make multi-smoke` (needs run-rag + `make tools-tokens` for `rag:obo`). metrics/deployment/knowledge are CODE ONLY (no LLM); knowledge stores POINTERS, never doc text. Outcome COMPLETE / PARTIAL / FAILED. Evidence is readable per kind (`EVIDENCE_KIND_PERMS`).
- Postmortem (Phase 12, ADR-022): `make pm-smoke` · `make postmortem INCIDENT=INC-n [ROLE=MANAGER]`. Timeline points are CODE (observations → `incident_events`, source = stored evidence kind); Sonnet (`reasoning`, fallback OFF) writes sentences only, each citing observations; code drops uncited / markup / unknown-number sentences; incident-service assembles the doc at READ time and redacts per reader. Times: always `astimezone(UTC)` before a "Z".
- History (Phase 13, ADR-023): `make embed-history` · `make history-eval` · `make history-smoke`. Incident search is hybrid (ACL in BOTH branches' SQL); the historical_incident agent keeps only `open_to_engineering` hits, stores POINTERS (INCIDENT), Haiku claims are kept only if code computes the same relation; the history prior is a TIE-BREAK inside a band, never shown to the ranker/critic.
- Code (Phase 14, ADR-024): `make codepack` · `make code-index` · `make code-smoke`. CODE ONLY (no model reads a diff): `code_intelligence` gets the commits each deploy before the first symptom shipped (`compare_commits`, AS THE USER), tree-sitter rules (`aeoi_codeparse`: query/remote call added in a loop, numeric limit changed, field removed), change impact via `find_references` (rag, file ACL in SQL). A finding is ONE more source for its own deploy (max one code observation per deploy); a removed field is impact only, never support. tree-sitter pinned to 0.23.x (last Intel-Mac wheels).
- Release risk (Phase 15, ADR-025): `make release-risk DEPLOY=|PR=|RANGE=` · `make risk-scan` (selectivity over every synthetic deploy + labelled cases; NOT accuracy) · `make risk-smoke`. ADVISORY: nothing gates a deploy on it. CODE ONLY: band = rubric over factors that cite evidence; a limit change is context (as risk it would flag ~40% of releases); unknown is never LOW, but `findings_band`/`coverage` say whether risk was FOUND or could not be checked; the way back says when a flag flip would NOT undo the code.
- Approval (Phase 16, ADR-026): `make approvals INCIDENT=` · `make approve ID= REASON= [DECISION=] [NTH=2]` · `make approval-smoke` (throwaway service, never a demo deploy) · `make token ROLE=INCIDENT_COMMANDER NTH=2` = a second IC. Request (SRE/IC, or an investigation's code rule FOR its starter) → an IC who is NOT the requester decides → the action runs in that request AS the approver (`service:action_executor` + the IC's own token; never delegated) → the gateway consumes the approval at incident-service (action + args sha256 + incident + approver, once), records the intent, runs, records → outcome EXECUTED → MITIGATED. Unknown result is never recorded as FAILED (retry `/execute`, safe). Tests never use real `secrets/` files or dev services.
- Identity (Phase 17a, ADR-027): `make keycloak-up` (realm GENERATED from identity.users, re-imported fresh) · `make keycloak-down` · `make kc-token ROLE=SRE [NTH=2]` · `make kc-smoke`. Every service's `Authenticator` routes by `iss`: Keycloak (USER tokens only; uid + aeoi_sub carry OUR directory ids, admin-only attributes) or the static dev issuer (service tokens; user tokens unless AEOI_STATIC_USER_TOKENS=false - production refuses to start otherwise). `aeoi-cli` is a dev-only password-grant client. Settings by field name now apply (`populate_by_name`).
- Console (Phase 17b, ADR-028): `make ui-install` · `make ui-dev` (:5173, Keycloak sign-in) · `make ui-check` (tsc + Vitest + types match) · `make ui-browser-test` (Playwright, real Keycloak) · `make ui-demo` / `ui-demo-clean` · `make openapi` after ANY api change (a unit test fails otherwise). Types come from `frontend/openapi.json` only. The UI gates, the server decides; untrusted text is never HTML.
- Consumers (Phase 18a, ADR-029): `make up PROFILE=kafka` · `make kafka-lag [GROUP=] [TOPIC=]` · `make kafka-dlq` · `make kafka-replay [EVENT=|OFFSET=] [DRY=1]` · `make consumer-smoke`. Every Kafka consumer uses `aeoi_messaging.Consumer`: commit AFTER the work, handler idempotent on event_id, poison → DLQ at once, dependency down → wait (never DLQ), else 3 tries → DLQ; replay max 3. A duplicate must never reach the DLQ. aiokafka hides outages (empty polls): health needs `transport.probe()`. Broker address: `AEOI_KAFKA_BOOTSTRAP_SERVERS` only.
- Containers (Phase 21, ADR-033): `make images` · `make apps-secrets-perms` (once) · `make apps-up` [AEOI_APP_MEM_LIMIT=2g to measure] · `make apps-peaks` · `make apps-stats` · `make apps-down`. macOS make is 3.81: it IGNORES `.SHELLFLAGS`, so a multi-command recipe must `set -eu -o pipefail` itself. ONE image `aeoi-py` for every Python service (non-root 10001, read-only FS, no uv); each container mounts ONLY the secret files its settings read; addresses are compose names (never localhost); `tests/unit/test_compose_contract.py` checks compose against the Settings classes. Debug one service: `docker compose stop tool-gateway`, then `make run-tools`.
- Kubernetes (Phase 22a, ADR-034): `make up` first (infra stays in compose) · `make images` · `make kind-up` · `make k8s-apply` · `make k8s-status` · `make k8s-netpol-test` · `make k8s-rollout-demo` · `make k8s-manifests` (render only, into `.k8s/`) · `make kind-down`. Manifests are GENERATED from `docker compose config` at deploy time (`scripts/k8s_manifests.py`): change compose, never a manifest; an unknown compose key is an error. Every kubectl call pins `--context kind-aeoi`. The cluster holds 127.0.0.1:8000-8010: compose apps / `make dev` need `make kind-down`. A rotated secret needs `kubectl rollout restart`. Claude never runs kind/kubectl: the owner does.
- Observability (Phase 23a, ADR-035): `make obs-up` (Grafana 127.0.0.1:3000) · `make obs-smoke [RESUME=kind|compose]` · `make obs-down` · `make obs-clean CONFIRM=1` · `make obs-load-peaks` (memory peaks WHILE three smokes run) · `make catalog-latency LABEL=…` (direct catalog timing). OFF unless `.env` sets `AEOI_OTEL_ENDPOINT=http://localhost:4318` (compose/kind map it to `otel-collector:4318`). Only via `aeoi_observability.telemetry`: work that runs LATER (an investigation, an outbox event) stores `current_traceparent()` and resumes with `continue_from()`; work started outside a request opens its own span (root client spans are dropped). Never put content in a span or log attribute: exceptions keep their TYPE only (DB errors carry rows). Dashboards are WRITTEN by `scripts/obs_dashboards.py` (edit it, run it; `--check` + a test catch drift) into `infrastructure/observability/grafana/dashboards/` (read-only in the UI) and may read only the columns granted to `obs_reader` (migrations 0026 + 0027; a test checks; a new column needs a grant migration).
- Catalog (Phase 20, ADR-032): `make catalog-build` · `make catalog-import` · `make db-users` · `make run-catalog` (:8010) · `make stop-catalog` · `make catalog-smoke` · `make catalog-contract`. CONTRACT FIRST: `services/service-catalog/openapi.yaml` is hand-written; change it, then `make catalog-contract`, then Java. The JSON file stays the tool's default; `AEOI_TOOLS_CATALOG_URL` switches to Java, and the smoke's parity check must show no difference. The sandbox cannot build Java: Java is verified on the Mac only.
- Reasoning (Phase 11, ADR-021): CODE proposes causes + rubric bands; Haiku (`fast`) only ranks/explains with refs; Sonnet (`reasoning`, fallback OFF) critiques blind to the ranker's prose; code validates. `make hypo-smoke`. A critic on the ranker's model or on a fallback = PARTIAL, never hidden.
- Agent pattern (ADR-018): facts by CODE citing tool evidence ids; LLM only labels/interprets, citations validated; uncitable → `notes`; prompt `services/agents/prompts/<agent>/vN.md` pinned by sha.
- Agents reach systems only via the tool-gateway (service token `tools:invoke` + `X-On-Behalf-Of` user token). New tool = `/new-tool` skill + contract lint + security test.
- Service code lives in `services/<name>/src/aeoi_<name>/` (unique package names: one venv for the workspace).
- Python: `uv` workspace — add a dep with `uv add --package aeoi-<lib> <dep>`; commit `uv.lock`.
- Work on a branch: the `no-commit-to-branch` hook blocks commits to `main`.

## Things Claude must never do here
- Put secrets in code, compose files, or committed `.env` — use `.env.local` (gitignored) / Docker secrets.
- Call an LLM SDK directly from an agent — always via the LLM Gateway client.
- Give an agent a raw DB/HTTP client to an "external" system — always via Tool Gateway.
- Invent evaluation/latency/cost numbers for docs, README or resume bullets.
- Run `kubectl`, `docker push`, or `git push` without the owner asking.

## Where things are
- Architecture: `architecture.md` · ADRs: `docs/adr/` · Roadmap: `docs/roadmap.md`
- Interview prep: `docs/interview/` · Demo script: `docs/demo/demo-scenario.md`
- Per-area instructions: nested `CLAUDE.md` in each `services/*`, `frontend/`, `infrastructure/`, `tests/`
