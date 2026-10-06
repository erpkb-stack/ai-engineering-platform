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

.PHONY: help doctor setup lint fmt typecheck test cov check hooks ports up down ps logs \
        verify-infra psql redis-cli kafka-topics kafka-init clean test-integration \
        db-users token run-incident run-api dev smoke \
        service-token ollama-pull run-llm stop-llm llm-smoke \
        db-upgrade db-downgrade db-verify db-current db-history db-check db-revision db-seed db-reset

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

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
	@echo "setup done -> next: make check && make up"

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
token: ## Print a dev JWT for a seeded user: export TOKEN=$$(make -s token ROLE=SRE)
	@uv run --quiet python -m aeoi_api.devtoken --role $(ROLE)

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
