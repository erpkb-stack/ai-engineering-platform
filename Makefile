# AEOI Platform - developer commands (macOS + Linux CI). `make help` lists them.
# Recipes use bash (macOS /bin/bash 3.2 compatible) so the same Makefile works in GitHub Actions.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# .env (ports, URLs) is exported to every recipe, so Python tools see AEOI_PG_PORT etc.
-include .env
export

PROFILE ?= infra
SEED ?= 42
COMPOSE := docker compose
ALEMBIC := uv run alembic -c libs/db/alembic.ini
ALL_PROFILES := --profile infra --profile kafka --profile tools
SVC ?=

ROLE ?= SRE
comma := ,
UVICORN := uv run uvicorn --factory --log-level warning

.PHONY: help doctor setup delegation-keys run-orch stop-orch orch-smoke lint fmt typecheck test cov check hooks ports up down ps logs \
        verify-infra psql redis-cli kafka-topics kafka-init clean test-integration \
        db-users token run-incident run-api dev smoke \
        service-token ollama-pull run-llm stop-llm llm-smoke \
        rag-token docpack rag-ingest rag-embed rag-stats rag-eval rag-sweep rag-bench stop-rag run-rag rag-smoke \
        tools-tokens run-tools stop-tools run-audit stop-audit tools-smoke \
        agents-tokens run-agents stop-agents hypo-smoke multi-smoke agent-run investigate agent-compare agent-status agent-smoke \
        db-upgrade db-downgrade db-verify db-current db-history db-check db-revision db-seed db-reset

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

# ---------- environment ----------
doctor: ## Check macOS prerequisites (read-only)
	@./scripts/doctor.sh

setup: ## One-time: Python env, git hooks, .env, local secrets
	@command -v uv >/dev/null || { echo "uv missing -> brew install uv"; exit 1; }
	uv sync --all-packages
	uv run pre-commit install
	@[ -f .env ] || { cp .env.example .env; echo "created .env"; }
	@mkdir -p secrets && chmod 700 secrets
	@# dir 700 protects it on your Mac; file 644 so the postgres user inside the container can read it
	@[ -f secrets/postgres_password.txt ] || { openssl rand -hex 24 > secrets/postgres_password.txt; \
	  chmod 644 secrets/postgres_password.txt; echo "created secrets/postgres_password.txt"; }
	@# Dev JWT signing keys (Phase 4). Production uses the IdP's keys (JWKS), never these.
	@[ -f secrets/jwt_private.pem ] || { openssl genrsa -out secrets/jwt_private.pem 2048 2>/dev/null; \
	  openssl rsa -in secrets/jwt_private.pem -pubout -out secrets/jwt_public.pem 2>/dev/null; \
	  chmod 600 secrets/jwt_private.pem; chmod 644 secrets/jwt_public.pem; echo "created dev JWT keypair"; }
	@$(MAKE) --no-print-directory delegation-keys
	@echo "setup done -> next: make check && make up"

delegation-keys: ## Phase 9: separate key pair for delegated tokens (ADR-019); idempotent
	@mkdir -p secrets && chmod 700 secrets
	@# SEPARATE from the user-token keys on purpose: only the api (STS) signs with it, and a
	@# delegated token is accepted only in the on-behalf-of slot, never as a bearer.
	@[ -f secrets/delegation_private.pem ] || { openssl genrsa -out secrets/delegation_private.pem 2048 2>/dev/null; \
	  openssl rsa -in secrets/delegation_private.pem -pubout -out secrets/delegation_public.pem 2>/dev/null; \
	  chmod 600 secrets/delegation_private.pem; chmod 644 secrets/delegation_public.pem; \
	  echo "created delegation keypair (secrets/delegation_*.pem)"; }

# ---------- code quality ----------
lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format and auto-fix
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy --strict on libs
	uv run mypy

test: ## Unit tests (no Docker needed)
	uv run pytest

cov: ## Unit tests with coverage
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test ## lint + typecheck + test (run before every commit)

test-integration: ## Integration tests against the compose Postgres (needs: make up)
	uv run pytest -m integration

hooks: ## Run all pre-commit hooks on all files
	uv run pre-commit run --all-files

# ---------- local infrastructure ----------
ports: ## Check host ports are free: make ports [PROFILE=infra|kafka] (default 5433/6380/9094)
	@./scripts/check-ports.sh $(PROFILE)

up: ## Start infra: make up [PROFILE=infra|kafka]
	@[ -f secrets/postgres_password.txt ] || { echo "run 'make setup' first"; exit 1; }
	@./scripts/check-ports.sh $(PROFILE)
	$(COMPOSE) --profile $(PROFILE) up -d --wait --wait-timeout 180
	@if [ "$(PROFILE)" != "infra" ]; then $(MAKE) --no-print-directory kafka-init; fi
	@$(COMPOSE) $(ALL_PROFILES) ps

kafka-init: ## Create Kafka topics (idempotent)
	@# kafka-init depends on kafka, so BOTH profiles must be enabled or compose can't resolve it
	$(COMPOSE) --profile kafka --profile tools run --rm kafka-init

down: ## Stop all containers (data volumes kept)
	$(COMPOSE) $(ALL_PROFILES) down --remove-orphans

ps: ## Container status
	$(COMPOSE) $(ALL_PROFILES) ps

logs: ## Follow logs: make logs [SVC=postgres]
	$(COMPOSE) $(ALL_PROFILES) logs -f --tail=100 $(SVC)

verify-infra: ## Smoke-test running infra (pgvector, roles, redis, kafka topics)
	@./scripts/verify-infra.sh

psql: ## psql shell into the aeoi database
	$(COMPOSE) exec postgres psql -U aeoi -d aeoi

redis-cli: ## redis-cli shell
	$(COMPOSE) exec redis redis-cli

kafka-topics: ## List Kafka topics
	$(COMPOSE) exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 --describe

clean: ## DESTROY containers + data volumes: make clean CONFIRM=1
	@[ "$${CONFIRM:-}" = "1" ] || [ "$(CONFIRM)" = "1" ] || { echo "This deletes all local DB/Kafka data. Re-run: make clean CONFIRM=1"; exit 1; }
	$(COMPOSE) $(ALL_PROFILES) down -v --remove-orphans

# ---------- database (Phase 3) ----------
db-upgrade: ## Apply all migrations (alembic upgrade head)
	$(ALEMBIC) upgrade head

db-downgrade: ## Roll back ONE migration
	$(ALEMBIC) downgrade -1

db-current: ## Show the applied migration
	$(ALEMBIC) current

db-history: ## List migrations
	$(ALEMBIC) history

db-check: ## Fail if ORM models and migrations have drifted
	$(ALEMBIC) check

db-revision: ## New migration: make db-revision MSG="add x" [SCHEMA=incident]
	@[ -n "$(MSG)" ] || { echo 'usage: make db-revision MSG="what changed" [SCHEMA=incident]'; exit 1; }
	@last=$$(ls libs/db/alembic/versions | grep -E '^[0-9]{4}_' | sort | tail -1 | cut -c1-4); \
	  next=$$(printf "%04d" $$((10#$$last + 1))); \
	  $(ALEMBIC) $(if $(SCHEMA),-x schema=$(SCHEMA)) revision --autogenerate --rev-id $$next -m "$(MSG)"
	@echo "READ and edit the generated file: autogenerate misses sequences, triggers, comments, grants."

db-verify: ## Print Phase 3 proof queries (counts, demo signals, permission filter)
	$(COMPOSE) exec -T postgres psql -U aeoi -d aeoi -v ON_ERROR_STOP=1 < scripts/sql/verify-phase3.sql

db-seed: ## Load deterministic synthetic data: make db-seed [SEED=42]
	uv run python -m aeoi_synth load --seed $(SEED)

db-reset: ## DESTROY + rebuild all AEOI tables, then seed: make db-reset CONFIRM=1
	@[ "$(CONFIRM)" = "1" ] || { echo "This drops ALL AEOI tables and data. Re-run: make db-reset CONFIRM=1"; exit 1; }
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head
	$(MAKE) --no-print-directory db-seed

db-users: ## Create/refresh least-privilege LOGIN users for services (passwords in secrets/)
	uv run python -m aeoi_db.users

# ---------- services (Phase 4) ----------
token: ## Dev JWT: export TOKEN=$$(make -s token ROLE=SRE) [GROUP=security-team] [WITHOUT=security-team]
	@uv run --quiet python -m aeoi_api.devtoken --role $(ROLE) $(if $(GROUP),--group $(GROUP),) \
	  $(if $(WITHOUT),--without-group $(WITHOUT),)

run-incident: ## Run incident-service on :8001 (auto-reload)
	$(UVICORN) aeoi_incident.main:build_app --port 8001 --reload --reload-dir services/incident-service/src --reload-dir libs

run-api: ## Run the api gateway on :8000 (auto-reload)
	$(UVICORN) aeoi_api.main:build_app --port 8000 --reload --reload-dir services/api/src --reload-dir libs

dev: ## Run incident-service + api together (Ctrl-C stops both)
	@trap 'kill 0' INT TERM EXIT; \
	  $(MAKE) --no-print-directory run-incident & \
	  $(MAKE) --no-print-directory run-api & \
	  wait

smoke: ## End-to-end check against running services (make dev in another terminal)
	@./scripts/smoke-phase4.sh

# ---------- LLM gateway (Phase 5) ----------
# LLM_ROUTING: routing.yaml (Claude + Ollama fallback) | routing.local.yaml ($0, offline) | routing.test.yaml (fakes)
LLM_ROUTING ?= routing.yaml
SCOPES ?= llm:invoke

service-token: ## Service JWT: export STOKEN=$$(make -s service-token SERVICE=orchestrator SCOPES=llm:invoke)
	@uv run --quiet python -m aeoi_api.devtoken --service $(SERVICE) $(foreach s,$(subst $(comma), ,$(SCOPES)),--scope $(s))

ollama-pull: ## Pull the local models used by routing.yaml (native Ollama app, not Docker)
	ollama pull llama3.2:3b
	ollama pull nomic-embed-text

LLM_PORT ?= 8005
LLM_PIDS = { lsof -nP -t -iTCP:$(LLM_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'

stop-llm: ## Stop a running llm-gateway (only if it really is ours)
	@pids=$$($(LLM_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(LLM_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q aeoi_llm; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 1; \
	  echo "stopped llm-gateway ($$pids)"; \
	else echo "port $(LLM_PORT) is used by something else (above). Not killing it."; exit 1; fi

run-llm: ## Run llm-gateway on :8005: make run-llm [LLM_ROUTING=routing.local.yaml]
	@pids=$$($(LLM_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(LLM_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-llm"; exit 1; fi
	AEOI_LLM_ROUTING_FILE=services/llm-gateway/config/$(LLM_ROUTING) \
	  $(UVICORN) aeoi_llm.main:build_app --port 8005 --reload --reload-dir services/llm-gateway --reload-dir libs

llm-smoke: ## Check a RUNNING llm-gateway (make run-llm in another terminal)
	@./scripts/smoke-phase5.sh

# ---------- RAG (Phase 6) ----------
RAG_PORT ?= 8004
RAG_PIDS = { lsof -nP -t -iTCP:$(RAG_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'
RAG := uv run --quiet python -m aeoi_rag

rag-token: ## Mint the rag -> llm-gateway service token (dev only, 30 days) into secrets/
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service rag --scope llm:invoke \
	  --ttl-minutes 43200 > secrets/rag_service_token.txt
	@echo "wrote secrets/rag_service_token.txt (scope llm:invoke, 30 days)"

docpack: ## Regenerate the document pack + eval queries (deterministic; commit the result)
	uv run python -m aeoi_synth docpack

rag-ingest: ## Ingest doc pack + seeded docs and embed everything (needs make run-llm)
	$(RAG) ingest
	$(RAG) chunk-db

rag-embed: ## Embed pending chunks only (resumable). REEMBED=1 after an embedding-model change
	$(RAG) embed $(if $(REEMBED),--reembed,)

rag-stats: ## Documents, chunks, pending embeddings, models
	@$(RAG) stats

rag-eval: ## Retrieval eval (recall@k, MRR, leakage, quarantine). RERANK=1 adds hybrid+rerank
	$(RAG) eval $(if $(RERANK),--rerank,)

rag-sweep: ## Fusion grid (rrf_k, depth, weights): tune on dev split, report on test split
	$(RAG) sweep

rag-bench: ## Embedding throughput (chunks/s) and query-embedding latency on this machine
	$(RAG) bench

stop-rag: ## Stop a running rag service (only if it really is ours)
	@pids=$$($(RAG_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(RAG_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q aeoi_rag; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 1; echo "stopped rag ($$pids)"; \
	else echo "port $(RAG_PORT) is used by something else (above). Not killing it."; exit 1; fi

run-rag: ## Run the rag service on :8004 (needs run-llm for vector search)
	@pids=$$($(RAG_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(RAG_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-rag"; exit 1; fi
	$(UVICORN) aeoi_rag.main:build_app --port $(RAG_PORT) --reload --reload-dir services/rag --reload-dir libs

rag-smoke: ## End-to-end search checks via the api gateway (needs run-llm, run-rag, run-api)
	@./scripts/smoke-phase6.sh

# ---------- Tool gateway + audit (Phase 7) ----------
TOOLS_PORT ?= 8006
AUDIT_PORT ?= 8008
TOOLS_PIDS = { lsof -nP -t -iTCP:$(TOOLS_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'
AUDIT_PIDS = { lsof -nP -t -iTCP:$(AUDIT_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'

tools-tokens: ## Mint the tool-gateway service tokens (-> audit: audit:write, -> rag: rag:obo), dev only, 30 days
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service tool-gateway --scope audit:write \
	  --ttl-minutes 43200 > secrets/tools_audit_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service tool-gateway --scope rag:obo \
	  --ttl-minutes 43200 > secrets/tools_rag_token.txt
	@echo "wrote secrets/tools_audit_token.txt (audit:write) and secrets/tools_rag_token.txt (rag:obo), 30 days"

stop-tools: ## Stop a running tool-gateway (only if it really is ours)
	@pids=$$($(TOOLS_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(TOOLS_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q aeoi_tools; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 1; echo "stopped tool-gateway ($$pids)"; \
	else echo "port $(TOOLS_PORT) is used by something else (above). Not killing it."; exit 1; fi

run-tools: ## Run the tool-gateway on :8006 (needs db-users, tools-tokens; run-rag for knowledge tools)
	@pids=$$($(TOOLS_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(TOOLS_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-tools"; exit 1; fi
	$(UVICORN) aeoi_tools.main:build_app --port $(TOOLS_PORT) --reload --reload-dir services/tool-gateway --reload-dir libs

stop-audit: ## Stop a running audit service (only if it really is ours)
	@pids=$$($(AUDIT_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(AUDIT_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q aeoi_audit; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 1; echo "stopped audit ($$pids)"; \
	else echo "port $(AUDIT_PORT) is used by something else (above). Not killing it."; exit 1; fi

run-audit: ## Run the audit service on :8008
	@pids=$$($(AUDIT_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(AUDIT_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-audit"; exit 1; fi
	$(UVICORN) aeoi_audit.main:build_app --port $(AUDIT_PORT) --reload --reload-dir services/audit --reload-dir libs

tools-smoke: ## End-to-end tool checks (needs run-api, run-tools, run-audit; run-rag for knowledge)
	@./scripts/smoke-phase7.sh

# ---------- Agents (Phase 8) + orchestrator service (Phase 9) ----------
AGENTS_PORT ?= 8003
AGENTS_PIDS = { lsof -nP -t -iTCP:$(AGENTS_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'
INCIDENT ?=
ROUTE ?=
ORCH := uv run --quiet python -m aeoi_orchestrator

agents-tokens: ## Mint dev service tokens: agents (tools+llm) and orchestrator (agents:run, evidence:write, hypotheses:write, delegation:create), 30 days
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service agents --scope tools:invoke \
	  --scope llm:invoke --ttl-minutes 43200 > secrets/agents_service_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service orchestrator --scope agents:run \
	  --scope evidence:write --scope hypotheses:write --scope delegation:create --ttl-minutes 43200 > secrets/orchestrator_service_token.txt
	@echo "wrote secrets/agents_service_token.txt and secrets/orchestrator_service_token.txt"

stop-agents: ## Stop a running agents service (only if it really is ours)
	@pids=$$($(AGENTS_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(AGENTS_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q aeoi_agents; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 1; echo "stopped agents ($$pids)"; \
	else echo "port $(AGENTS_PORT) is used by something else (above). Not killing it."; exit 1; fi

run-agents: ## Run the agents worker on :8003 (needs run-tools, run-llm)
	@pids=$$($(AGENTS_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(AGENTS_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-agents"; exit 1; fi
	$(UVICORN) aeoi_agents.main:build_app --port $(AGENTS_PORT) --reload --reload-dir services/agents --reload-dir libs

ORCH_PORT ?= 8002
ORCH_PIDS = { lsof -nP -t -iTCP:$(ORCH_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'

run-orch: ## Run the orchestrator service on :8002 (needs dev, run-agents; resumes RUNNING investigations on start)
	@pids=$$($(ORCH_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(ORCH_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-orch"; exit 1; fi
	@[ -f secrets/delegation_public.pem ] || { echo "missing delegation keys - run: make delegation-keys (then restart make dev)"; exit 1; }
	$(UVICORN) aeoi_orchestrator.main:build_app --port $(ORCH_PORT) --reload --reload-dir services/orchestrator --reload-dir libs

stop-orch: ## Stop the orchestrator (RUNNING investigations stay RUNNING and resume on the next run-orch)
	@pids=$$($(ORCH_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(ORCH_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q aeoi_orchestrator; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 1; echo "stopped orchestrator ($$pids)"; \
	else echo "port $(ORCH_PORT) is used by something else (above). Not killing it."; exit 1; fi

orch-smoke: ## End-to-end Phase 9 check incl. a crash + resume (needs dev, run-llm, run-tools, run-audit, run-agents, run-orch)
	@./scripts/smoke-phase9.sh

hypo-smoke: ## End-to-end Phase 11 check: hypotheses + critic + validation (Phase 10 services; Haiku on fast, Sonnet on reasoning)
	@./scripts/smoke-phase11.sh

multi-smoke: ## End-to-end Phase 10 check: 4 agents in parallel (needs dev, run-llm, run-rag, run-tools, run-audit, run-agents, run-orch)
	@./scripts/smoke-phase10.sh

agent-run: ## Run the Log Analysis agent on an incident: make agent-run INCIDENT=INC-10001 [ROLE=SRE] [ROUTE=local|fast] [NOCACHE=1]
	@test -n "$(INCIDENT)" || { echo "usage: make agent-run INCIDENT=INC-10001 [ROUTE=local|fast]"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE)) $(ORCH) run-log-agent $(INCIDENT) $(if $(ROUTE),--route $(ROUTE),) $(if $(NOCACHE),--no-cache,)

investigate: ## Phase 10: all agents in parallel on an incident: make investigate INCIDENT=INC-10001 [AGENTS=metrics,deployment] [NOCACHE=1]
	@test -n "$(INCIDENT)" || { echo "usage: make investigate INCIDENT=INC-10001 [AGENTS=a,b]"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE)) $(ORCH) investigate $(INCIDENT) $(if $(AGENTS),--agents $(AGENTS),) $(if $(ROUTE),--route $(ROUTE),) $(if $(NOCACHE),--no-cache,)

agent-compare: ## Same incident, two models (local llama vs Claude Haiku): make agent-compare INCIDENT=INC-10001
	@test -n "$(INCIDENT)" || { echo "usage: make agent-compare INCIDENT=INC-10001"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE)) $(ORCH) compare $(INCIDENT)

agent-status: ## Investigations of an incident: make agent-status INCIDENT=INC-10001
	@test -n "$(INCIDENT)" || { echo "usage: make agent-status INCIDENT=INC-10001"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE) 2>/dev/null) $(ORCH) status $(INCIDENT)

agent-smoke: ## End-to-end Phase 8 check (needs dev, run-llm, run-tools, run-audit, run-agents)
	@./scripts/smoke-phase8.sh
