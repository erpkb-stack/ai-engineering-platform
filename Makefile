# AEOI Platform - developer commands (macOS + Linux CI). `make help` lists them.
# Recipes use bash (macOS /bin/bash 3.2 compatible) so the same Makefile works in GitHub Actions.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PROFILE ?= infra
COMPOSE := docker compose
ALL_PROFILES := --profile infra --profile kafka --profile tools
SVC ?=

.PHONY: help doctor setup lint fmt typecheck test cov check hooks ports up down ps logs \
        verify-infra psql redis-cli kafka-topics kafka-init clean

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
