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
ALL_PROFILES := --profile infra --profile kafka --profile tools --profile apps --profile apps-catalog --profile redis
SVC ?=

ROLE ?= SRE
comma := ,
UVICORN := uv run uvicorn --factory --log-level warning

.PHONY: catalog-contract catalog-build catalog-import run-catalog stop-catalog catalog-smoke
.PHONY: images apps-up apps-secrets-perms apps-down apps-stats apps-peaks
.PHONY: kind-up kind-down k8s-manifests k8s-apply k8s-status k8s-netpol-test k8s-rollout-demo
.PHONY: obs-up obs-down obs-clean obs-smoke obs-load-peaks catalog-latency chaos demo demo-clean demo-fresh
.PHONY: help doctor setup delegation-keys run-orch stop-orch orch-smoke lint fmt typecheck test cov check hooks ports up down ps logs \
        verify-infra psql redis-cli kafka-topics kafka-init kafka-lag kafka-dlq kafka-replay consumer-smoke clean test-integration \
        db-users token run-incident run-api dev smoke \
        service-token ollama-pull run-llm stop-llm llm-smoke \
        rag-token docpack codepack rag-ingest rag-embed rag-stats rag-eval rag-sweep rag-bench stop-rag run-rag rag-smoke \
        embed-history history-eval history-explain code-index \
        tools-tokens run-tools stop-tools run-audit stop-audit tools-smoke \
        agents-tokens run-agents stop-agents hypo-smoke pm-smoke history-smoke code-smoke release-risk risk-scan risk-smoke approvals approve approval-smoke keycloak-realm keycloak-up keycloak-down kc-token kc-smoke openapi ui-install ui-dev ui-check ui-browser-test ui-demo ui-demo-clean postmortem multi-smoke agent-run investigate agent-compare agent-status agent-smoke \
        db-upgrade db-downgrade db-verify db-current db-history db-check db-revision db-seed db-reset

help: ## List targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

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

up: ## Start infra: make up [PROFILE=infra|kafka] (PROFILE=redis ADDS redis only: no service uses it, ADR-031)
	@[ -f secrets/postgres_password.txt ] || { echo "run 'make setup' first"; exit 1; }
	@./scripts/check-ports.sh $(PROFILE)
	$(COMPOSE) --profile $(PROFILE) up -d --wait --wait-timeout 180
	@if [ "$(PROFILE)" != "infra" ] && [ "$(PROFILE)" != "redis" ]; then $(MAKE) --no-print-directory kafka-init; fi
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

verify-infra: ## Smoke-test running infra (pgvector, roles, kafka topics; redis only if started: PROFILE=redis)
	@./scripts/verify-infra.sh

psql: ## psql shell into the aeoi database
	$(COMPOSE) exec postgres psql -U aeoi -d aeoi

redis-cli: ## redis-cli shell
	$(COMPOSE) exec redis redis-cli

kafka-topics: ## List Kafka topics
	$(COMPOSE) exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 --describe

# ---------- Consumers (Phase 18a, ADR-029) - needs make up PROFILE=kafka ----------
MSG := uv run --quiet python -m aeoi_messaging
kafka-lag: ## Consumer lag: make kafka-lag [GROUP=audit] [TOPIC=incident.lifecycle]
	@$(MSG) lag --group $(or $(GROUP),audit) --topic $(or $(TOPIC),incident.lifecycle)

kafka-dlq: ## List dead letters: make kafka-dlq [TOPIC=incident.lifecycle.dlq]
	@$(MSG) dlq --topic $(or $(TOPIC),incident.lifecycle.dlq)

kafka-replay: ## Replay dead letters AFTER a fix: make kafka-replay [TOPIC=..dlq] [EVENT=<event id>|OFFSET=n] [DRY=1]
	@$(MSG) replay --topic $(or $(TOPIC),incident.lifecycle.dlq) $(if $(EVENT),--event-id $(EVENT),) $(if $(OFFSET),--offset $(OFFSET),) $(if $(DRY),--dry-run,)

consumer-smoke: ## End-to-end Phase 18a check: incident events -> Kafka -> audit, once; poison -> DLQ; replay; lag 0
	@./scripts/smoke-phase18a.sh

# ---------- service catalog (Phase 20, ADR-005 + ADR-032; Java 21 + Maven, Docker for tests) ----------
CATALOG_PORT ?= 8010
CATALOG_DIR := services/service-catalog
CATALOG_JAR := $(CATALOG_DIR)/target/service-catalog.jar
CATALOG_PIDS = { lsof -nP -t -iTCP:$(CATALOG_PORT) -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ',' | sed 's/,$$//'
# JDK 21 for Maven: Spring Boot 3.4 supports Java 17-24, and the Mac's Maven picked JDK 25.
# macOS: java_home -v 21; elsewhere (or no JDK 21 installed): whatever JAVA_HOME already is
CATALOG_JAVA_HOME = $$( /usr/libexec/java_home -v 21 2>/dev/null || echo "$$JAVA_HOME" )
# owner credentials (migrations + import only); the serving process gets catalog_svc's
PG_OWNER_PW = $$(cat $${AEOI_PG_PASSWORD_FILE:-secrets/postgres_password.txt})

catalog-contract: ## Regenerate the Python models from services/service-catalog/openapi.yaml (after ANY contract change)
	uv run --quiet python scripts/catalog_contract.py

catalog-build: ## Build + test the Java service (mvn verify; tests use the compose Postgres: make up) -> target/service-catalog.jar
	@JAVA_HOME="$(CATALOG_JAVA_HOME)"; export JAVA_HOME; echo "JDK for the build: $$JAVA_HOME"; \
	AEOI_REPO_ROOT="$(CURDIR)" mvn -q -B -f $(CATALOG_DIR)/pom.xml verify

catalog-import: ## Migrate (Flyway) + load data/generated/catalog.json, as the schema owner, then exit (dev only)
	@[ -f $(CATALOG_JAR) ] || { echo "no $(CATALOG_JAR): run make catalog-build"; exit 1; }
	@[ -f data/generated/catalog.json ] || { echo "no data/generated/catalog.json: run make db-seed"; exit 1; }
	@CATALOG_MIGRATE=true CATALOG_DB_USER=$${AEOI_PG_USER:-aeoi} CATALOG_DB_PASSWORD="$(PG_OWNER_PW)" \
	  AEOI_REPO_ROOT="$(CURDIR)" java -jar $(CATALOG_JAR) --spring.main.web-application-type=none \
	  "--catalog.import-file=$(CURDIR)/data/generated/catalog.json"
	@echo "-> make db-users (creates the catalog_svc login the first time), then make run-catalog"

run-catalog: ## Run the service catalog on :8010 as catalog_svc (SELECT only); needs catalog-import + db-users
	@pids=$$($(CATALOG_PIDS)); if [ -n "$$pids" ]; then \
	  echo "port $(CATALOG_PORT) is already in use by:"; ps -o pid=,command= -p $$pids; \
	  echo "-> run: make stop-catalog"; exit 1; fi
	@[ -f secrets/catalog_svc_password.txt ] || { echo "no catalog_svc login: make catalog-import, then make db-users"; exit 1; }
	@CATALOG_DB_PASSWORD="$$(cat secrets/catalog_svc_password.txt)" AEOI_REPO_ROOT="$(CURDIR)" \
	  java -Xmx256m -jar $(CATALOG_JAR)

stop-catalog: ## Stop a running service catalog (only if it really is ours)
	@pids=$$($(CATALOG_PIDS)); \
	if [ -z "$$pids" ]; then echo "port $(CATALOG_PORT) is free"; exit 0; fi; \
	ps -o pid=,command= -p $$pids; \
	if ps -o command= -p $$pids | grep -q service-catalog.jar; then \
	  kill $$(echo $$pids | tr ',' ' '); sleep 2; echo "stopped service-catalog ($$pids)"; \
	else echo "port $(CATALOG_PORT) is used by something else (above). Not killing it."; exit 1; fi

catalog-smoke: ## End-to-end Phase 20 check: the Java catalog answers the tool exactly like the JSON file
	@./scripts/smoke-phase20.sh

# ---------- containers for the apps (Phase 21, ADR-033) ----------
# ONE image for every Python service + one for the Java catalog. Profiles follow YOUR .env
# switches AS THE CONTAINERS SEE THEM (read from `docker compose config`, review #8): the Java
# catalog only with AEOI_TOOLS_CATALOG_URL, Kafka only with the Kafka transport or the audit
# consumer on. Keycloak stays `make keycloak-up`.
IMAGE_TAG ?= dev
APP_SERVICES := api incident-service orchestrator agents rag llm-gateway tool-gateway audit
APPS_PY := uv run --quiet python scripts/phase21_apps.py
APPS_CFG := .apps-config.json

images: ## Build aeoi-py (all Python services) and aeoi-catalog (Java) [IMAGE_TAG=dev]
	docker build -f infrastructure/docker/python.Dockerfile -t aeoi-py:$(IMAGE_TAG) .
	docker build -f infrastructure/docker/catalog.Dockerfile -t aeoi-catalog:$(IMAGE_TAG) .
	@docker image ls --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' | grep -E '^(REPOSITORY|aeoi-)'

apps-up: ## Start the apps as containers on the usual ports [AEOI_APP_MEM_LIMIT=2g to measure]
	@docker info > /dev/null 2>&1 || { echo "Docker is not running: start Docker Desktop"; exit 1; }
	@# set -e HERE, not via .SHELLFLAGS: macOS make 3.81 ignores .SHELLFLAGS, and this recipe once
	@# printed "nothing started" and then started everything (Phase 21, found on the Mac)
	@set -eu -o pipefail; export AEOI_IMAGE_TAG=$(IMAGE_TAG); trap 'rm -f $(APPS_CFG)' EXIT; \
	  $(COMPOSE) --profile apps config --format json > $(APPS_CFG); \
	  extra="$$($(APPS_PY) profiles $(APPS_CFG))"; \
	  $(COMPOSE) --profile apps $$extra config --format json > $(APPS_CFG); \
	  running="$$($(COMPOSE) --profile apps $$extra ps --services --status running 2>/dev/null | tr '\n' ',')"; \
	  $(APPS_PY) check $(APPS_CFG) --running "$$running"; \
	  echo "profiles: apps $$extra"; \
	  if echo "$$extra" | grep -q kafka; then \
	    $(COMPOSE) --profile kafka up -d --wait --wait-timeout 180 kafka; \
	    $(MAKE) --no-print-directory kafka-init; \
	  fi; \
	  $(COMPOSE) --profile apps $$extra up -d --wait --wait-timeout 300; \
	  $(COMPOSE) --profile apps $$extra ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

apps-secrets-perms: ## Let the containers (uid 10001) read THEIR secret files: files 644, secrets/ stays 700
	@set -eu -o pipefail; trap 'rm -f $(APPS_CFG)' EXIT; \
	  $(COMPOSE) --profile apps --profile apps-catalog config --format json > $(APPS_CFG); \
	  $(APPS_PY) fix-perms $(APPS_CFG)

apps-down: ## Stop + remove the app containers (infra keeps running)
	$(COMPOSE) $(ALL_PROFILES) rm -sf $(APP_SERVICES) service-catalog

apps-stats: ## Memory + CPU of every container, now
	@docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'

apps-peaks: ## PEAK memory per container while you run the smokes: make apps-peaks [SECONDS=600]
	@$(APPS_PY) peaks --seconds $(or $(SECONDS),600)

# ---------- Kubernetes on kind (Phase 22a, ADR-034) ----------
# Only the APPS run in kind; postgres/kafka/keycloak stay in compose (make up first). Manifests
# are GENERATED from docker-compose.yml at deploy time (scripts/k8s_manifests.py) into .k8s/.
# Every kubectl call names --context kind-aeoi: your current kubectl context is never used.
# The cluster holds 127.0.0.1:8000-8010 while it exists: make kind-down frees them.
K8S := uv run --quiet python scripts/k8s_deploy.py

kind-up: ## Create the kind cluster `aeoi` and join it to the compose network (needs make up)
	@$(K8S) up

kind-down: ## Delete the kind cluster (the compose infra keeps running)
	@$(K8S) down

k8s-manifests: ## Write the manifests to .k8s/manifests to READ them (nothing is applied)
	@$(K8S) render

k8s-apply: ## Load images, create Secrets from secrets/, apply, wait until every app is available
	@$(K8S) apply

k8s-status: ## Deployments, pods (+ restarts), services, policies, node memory
	@$(K8S) status

k8s-netpol-test: ## Every pod tries every target: must equal the allowed graph (forbidden = closed)
	@$(K8S) netpol-test

k8s-rollout-demo: ## Restart api + incident-service under traffic: must be 0 failed requests
	@set -eu -o pipefail; \
	  TOKEN="$$(uv run --quiet python -m aeoi_api.devtoken --role SRE)"; export TOKEN; \
	  $(K8S) rollout-demo

# ---------- observability (Phase 23a, ADR-035) ----------
# Collector + Tempo + Prometheus + Loki + Grafana in compose. The apps export ONLY when .env sets
# AEOI_OTEL_ENDPOINT=http://localhost:4318 (host runs use it as written; compose and kind replace
# it with http://otel-collector:4318). Then make apps-up or make k8s-apply again.
OBS_PY := uv run --quiet python scripts/obs_smoke.py

obs-up: ## Start the observability stack; Grafana on http://127.0.0.1:3000 (admin, secrets/grafana_admin_password.txt)
	@set -eu -o pipefail; \
	  if [ ! -s secrets/grafana_ro_password.txt ]; then \
	    echo "missing secrets/grafana_ro_password.txt: make db-upgrade, then make db-users"; exit 1; fi; \
	  if [ ! -s secrets/grafana_admin_password.txt ]; then \
	    ( umask 077; uv run --quiet python -c "import secrets; print(secrets.token_urlsafe(18))" \
	        > secrets/grafana_admin_password.txt ); echo "created secrets/grafana_admin_password.txt"; fi; \
	  trap 'rm -f $(APPS_CFG)' EXIT; \
	  $(COMPOSE) --profile observability config --format json > $(APPS_CFG); \
	  $(APPS_PY) fix-perms $(APPS_CFG); \
	  $(COMPOSE) --profile observability up -d; \
	  $(OBS_PY) --wait-stack

obs-down: ## Stop the observability stack (keeps its data volumes)
	$(COMPOSE) --profile observability stop otel-collector tempo prometheus loki grafana

obs-clean: ## Remove the stack AND its data (traces, logs, metrics): make obs-clean CONFIRM=1
	@[ "$(CONFIRM)" = "1" ] || { echo "This deletes all traces, logs and metrics. Re-run: make obs-clean CONFIRM=1"; exit 1; }
	$(COMPOSE) --profile observability rm -sf otel-collector tempo-init tempo prometheus loki grafana
	@for v in aeoi_tempodata aeoi_promdata aeoi_lokidata aeoi_grafanadata; do \
	  docker volume rm "$$v" >/dev/null 2>&1 && echo "removed $$v" || echo "no $$v"; done

obs-smoke: ## One investigation = ONE trace + its logs + metrics [RESUME=kind|compose: restart the orchestrator mid-run]
	@set -eu -o pipefail; \
	  TOKEN="$$(uv run --quiet python -m aeoi_api.devtoken --role SRE)"; export TOKEN; \
	  $(OBS_PY) $(if $(RESUME),--resume $(RESUME),)

obs-load-peaks: ## Phase 23c: PEAK memory of every container WHILE obs-smoke RESUME=kind, multi-smoke, risk-smoke run
	@$(APPS_PY) peaks --during "obs-smoke RESUME=kind" --during multi-smoke --during risk-smoke

catalog-latency: ## Phase 23c: time 300 direct catalog requests: make catalog-latency LABEL=agent-on|agent-off
	@set -eu -o pipefail; \
	  [ -n "$(LABEL)" ] || { echo "usage: make catalog-latency LABEL=agent-on|agent-off"; exit 1; }; \
	  uv run --quiet python scripts/catalog_latency.py --label "$(LABEL)"

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
token: ## Dev JWT: export TOKEN=$$(make -s token ROLE=SRE) [GROUP=security-team] [WITHOUT=security-team] [NTH=2]
	@uv run --quiet python -m aeoi_api.devtoken --role $(ROLE) $(if $(GROUP),--group $(GROUP),) \
	  $(if $(WITHOUT),--without-group $(WITHOUT),) $(if $(NTH),--nth $(NTH),)

run-incident: ## Run incident-service on :8001 (auto-reload)
	$(UVICORN) aeoi_incident.main:build_app --port 8001 --reload --reload-dir services/incident-service/src --reload-dir libs

run-api: ## Run the api gateway on :8000 (auto-reload)
	$(UVICORN) aeoi_api.main:build_app --port 8000 --reload --reload-dir services/api/src --reload-dir libs

dev: ## Run incident-service + api together (Ctrl-C stops both)
	@trap 'kill 0' INT TERM EXIT; \
	  $(MAKE) --no-print-directory run-incident & \
	  $(MAKE) --no-print-directory run-api & \
	  wait

smoke: ## End-to-end check against running services (make dev + make run-orch, or make apps-up)
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

codepack: ## Phase 14: write the synthetic repositories at HEAD to data/sample-repositories (deterministic)
	uv run python -m aeoi_synth codepack

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

embed-history: ## Phase 13: vectors for the 600 past incidents (needs make run-llm). REEMBED=1 clears all first
	$(RAG) embed-history $(if $(REEMBED),--reembed,)

code-index: ## Phase 14: parse data/sample-repositories with tree-sitter into rag.code_* (run make codepack first)
	$(RAG) index-code

history-explain: ## Phase 13 debug: why each past incident ranked: make history-explain Q="..." [SERVICE=checkout-api]
	@test -n "$(Q)" || { echo 'usage: make history-explain Q="pool timeouts after deploy" [SERVICE=checkout-api]'; exit 2; }
	$(RAG) history-explain --query "$(Q)" $(if $(SERVICE),--service $(SERVICE),)

eval-seed: ## Phase 25: load the investigation scenario packs (v1: 16 planted causes, v2: 6 flaw-(c) cases) ON TOP of db-seed [SEED=42]
	uv run python -m aeoi_synth eval-pack --seed $(SEED)

eval-investigations: ## Phase 25a: product-path eval vs planted causes [VERSION=2] [CASES=S01,S11] [COST=1] [BASELINE=file] (needs the services)
	uv run python scripts/eval_investigations.py --seed $(SEED) $(if $(VERSION),--version $(VERSION),) $(if $(CASES),--cases $(CASES),) \
	  $(if $(COST),--allow-cost,) $(if $(BASELINE),--baseline $(BASELINE),)

eval-rescore: ## Phase 25b: score a past eval run again from what was stored (free, no model): make eval-rescore FILE=data/eval/results/x.json
	@test -n "$(FILE)" || { echo "usage: make eval-rescore FILE=data/eval/results/investigations-....json"; exit 2; }
	uv run python scripts/eval_investigations.py --rescore $(FILE)

eval-calibration: ## Phase 25b: band/status calibration (Wilson 95%) of a run [VS=second run to compare]: make eval-calibration RUN=file
	@test -n "$(RUN)" || { echo "usage: make eval-calibration RUN=data/eval/results/x.json [VS=y.json]"; exit 2; }
	uv run python scripts/eval_calibration.py --run $(RUN) $(if $(VS),--vs $(VS),)

eval-import: ## Phase 25c: copy eval result files into the eval schema for the Evaluation dashboard [FILES="a.json b.json"] (default: all; refused kinds named)
	uv run python scripts/eval_import.py $(FILES)

load: ## Phase 27: load the DEPLOYED api as the seeded team: make load PLAN=smoke|policy|capacity|investigations [API=http://localhost:8000] (fake LLM only)
	@set -eu; test -n "$(PLAN)" || { echo "usage: make load PLAN=smoke|policy|capacity|investigations"; exit 2; }; \
	AEOI_LOAD_PLAN=$(PLAN) uv run --group load locust -f tests/load/locustfile.py --headless \
	  --host $(or $(API),http://localhost:8000) --only-summary --loglevel WARNING --exit-code-on-error 0

demo: ## Phase 29: the demo script end to end on the DEPLOYED system, every step checked (fake models = rehearsal, real = a counted pass); then: make demo-clean
	uv run python tests/demo/run.py

demo-fresh: ## Phase 29: the "clean make up" before a counted pass: fresh infra containers + fresh app pods, DATA KEPT (no db-seed: it truncates rag + history). [LLM_ROUTING=routing.load.yaml to rehearse]
	@set -eu -o pipefail; $(MAKE) --no-print-directory down; $(MAKE) --no-print-directory up PROFILE=kafka; \
	$(MAKE) --no-print-directory k8s-apply; \
	kubectl --context kind-aeoi -n ai-engineering-platform rollout restart deployment; \
	for d in $$(kubectl --context kind-aeoi -n ai-engineering-platform get deploy -o name); do \
	  kubectl --context kind-aeoi -n ai-engineering-platform rollout status $$d --timeout=300s; done; \
	echo "waiting 180 s: a rollout stalls this node (Phase 28a finding 2) - the demo starts on a quiet one"; \
	sleep 180; $(MAKE) --no-print-directory k8s-status

demo-clean: ## Phase 29: remove the demo's rollback deploy row (DEPLOY-4821 running again); incidents + audit stay
	uv run python tests/demo/run.py clean

chaos: ## Phase 28a: ONE failure experiment on the deployed system: make chaos EXP=pg|kafka|orchestrator|agents (stops a container / deletes a pod; fake LLM only)
	@set -eu; test -n "$(EXP)" || { echo "usage: make chaos EXP=pg|kafka|orchestrator|agents"; exit 2; }; \
	uv run python tests/chaos/run.py $(EXP)

history-eval: ## Phase 13: similar-incident eval, keyword vs vector vs hybrid (data/eval/results/history-*.json)
	$(RAG) history-eval

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

tools-tokens: ## Mint the tool-gateway service tokens (-> audit: audit:write, -> rag: rag:obo, -> incident: approvals:consume, -> catalog: catalog:read), dev only, 30 days
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service tool-gateway --scope audit:write \
	  --ttl-minutes 43200 > secrets/tools_audit_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service tool-gateway --scope rag:obo \
	  --ttl-minutes 43200 > secrets/tools_rag_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service tool-gateway --scope approvals:consume \
	  --ttl-minutes 43200 > secrets/tools_incident_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service tool-gateway --scope catalog:read \
	  --ttl-minutes 43200 > secrets/tools_catalog_token.txt
	@echo "wrote secrets/tools_audit_token.txt (audit:write), secrets/tools_rag_token.txt (rag:obo), secrets/tools_incident_token.txt (approvals:consume), secrets/tools_catalog_token.txt (catalog:read), 30 days"

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

agents-tokens: ## Mint dev service tokens: agents (tools+llm), action_executor (tools, Phase 16) and orchestrator (agents:run, evidence:write, hypotheses:write, timeline:write, postmortems:write, delegation:create, actions:propose, actions:record), 30 days
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service agents --scope tools:invoke \
	  --scope llm:invoke --ttl-minutes 43200 > secrets/agents_service_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service orchestrator --scope agents:run \
	  --scope evidence:write --scope hypotheses:write --scope timeline:write --scope postmortems:write \
	  --scope delegation:create --scope actions:propose --scope actions:record \
	  --ttl-minutes 43200 > secrets/orchestrator_service_token.txt
	@umask 077; uv run --quiet python -m aeoi_api.devtoken --service action_executor --scope tools:invoke \
	  --ttl-minutes 43200 > secrets/action_executor_token.txt
	@echo "wrote secrets/agents_service_token.txt, secrets/orchestrator_service_token.txt, secrets/action_executor_token.txt"

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

pm-smoke: ## End-to-end Phase 12 check: timeline + postmortem (Sonnet narrative) on the planted demo
	@./scripts/smoke-phase12.sh

code-smoke: ## End-to-end Phase 14 check: code index + code_intelligence agent + COMMIT evidence (needs codepack, code-index)
	@./scripts/smoke-phase14.sh

history-smoke: ## End-to-end Phase 13 check: hybrid incident search + historical_incident agent + priors (needs make embed-history)
	@./scripts/smoke-phase13.sh

postmortem: ## Print the latest postmortem of an incident as markdown: make postmortem INCIDENT=INC-10001 [ROLE=SRE]
	@test -n "$(INCIDENT)" || { echo "usage: make postmortem INCIDENT=INC-10001 [ROLE=SRE]"; exit 2; }
	@curl -sf "$${AEOI_API_URL:-http://localhost:8000}/api/v1/incidents/$(INCIDENT)/postmortem?format=markdown" \
	  -H "Authorization: Bearer $$($(MAKE) -s token ROLE=$(ROLE))" || echo "no postmortem for $(INCIDENT) (or no access)"

hypo-smoke: ## End-to-end Phase 11 check: hypotheses + critic + validation (Phase 10 services; Haiku on fast, Sonnet on reasoning)
	@./scripts/smoke-phase11.sh

multi-smoke: ## End-to-end Phase 10 check: 4 agents in parallel (needs dev, run-llm, run-rag, run-tools, run-audit, run-agents, run-orch)
	@./scripts/smoke-phase10.sh

agent-run: ## Run the Log Analysis agent on an incident: make agent-run INCIDENT=INC-10001 [ROLE=SRE] [ROUTE=local|fast] [NOCACHE=1]
	@test -n "$(INCIDENT)" || { echo "usage: make agent-run INCIDENT=INC-10001 [ROUTE=local|fast]"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE)) $(ORCH) run-log-agent $(INCIDENT) $(if $(ROUTE),--route $(ROUTE),) $(if $(NOCACHE),--no-cache,)

release-risk: ## Phase 15: advisory release risk: make release-risk DEPLOY=DEPLOY-4821 | PR=northwind/checkout#812 | RANGE=svc:BASE..HEAD [ROLE=SRE] [JSON=1]
	@test -n "$(DEPLOY)$(PR)$(RANGE)" || { echo "usage: make release-risk DEPLOY=DEPLOY-4821 | PR=northwind/checkout#812 | RANGE=checkout-api:BASE..HEAD"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE)) $(ORCH) release-risk $(if $(DEPLOY),--deploy $(DEPLOY),) $(if $(PR),--pr '$(PR)',) $(if $(RANGE),--range '$(RANGE)',) $(if $(JSON),--json,)

risk-scan: ## Phase 15: band distribution of the rubric over EVERY synthetic deploy (offline; selectivity, not accuracy)
	uv run python -m aeoi_agents.release.scan

risk-smoke: ## End-to-end Phase 15 check: release risk for the demo deploy + PR, advisory, MANAGER denied
	@./scripts/smoke-phase15.sh

approvals: ## Phase 16: the decision record of an incident: make approvals INCIDENT=INC-10001 [ROLE=SRE (default)|INCIDENT_COMMANDER|MANAGER]
	@test -n "$(INCIDENT)" || { echo "usage: make approvals INCIDENT=INC-10001"; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=$(ROLE)) $(ORCH) approvals $(INCIDENT)

approve: ## Phase 16: decide as an IC: make approve ID=<approval id> [DECISION=APPROVED|REJECTED|MORE_INFO] REASON="..." [NTH=2]
	@test -n "$(ID)" -a -n "$(REASON)" || { echo 'usage: make approve ID=<approval id> REASON="why" [DECISION=REJECTED] [NTH=2]'; exit 2; }
	@AEOI_USER_TOKEN=$$($(MAKE) -s token ROLE=INCIDENT_COMMANDER NTH=$(or $(NTH),2)) $(ORCH) decide $(ID) --decision $(or $(DECISION),APPROVED) --reason '$(REASON)'

approval-smoke: ## End-to-end Phase 16 check: proposal -> other IC approves -> rollback -> MITIGATED; refusals
	@./scripts/smoke-phase16.sh

# ---------- Identity provider (Phase 17, ADR-027) ----------
keycloak-realm: ## Generate the Keycloak realm FROM identity.users (+ admin password), into secrets/keycloak/
	@[ -f secrets/keycloak_admin_password.txt ] || { umask 022; openssl rand -hex 16 > secrets/keycloak_admin_password.txt; \
	  chmod 644 secrets/keycloak_admin_password.txt; echo "created secrets/keycloak_admin_password.txt"; }
	uv run python scripts/keycloak_realm.py

keycloak-up: keycloak-realm ## Start Keycloak on :$(AEOI_KC_PORT) with a FRESH import of the generated realm
	$(COMPOSE) --profile auth up -d --force-recreate --wait --wait-timeout 480 keycloak
	@echo "issuer: http://localhost:$${AEOI_KC_PORT:-8090}/realms/aeoi  (admin console: /admin, user admin, password in secrets/keycloak_admin_password.txt)"

keycloak-down: ## Stop Keycloak (frees ~0.6 GB, limit 1 GiB)
	$(COMPOSE) --profile auth stop keycloak

kc-token: ## Keycloak access token (dev-only aeoi-cli client): export KT=$$(make -s kc-token ROLE=SRE [NTH=2])
	@uv run --quiet python scripts/kc_token.py --role $(ROLE) $(if $(NTH),--nth $(NTH),)

kc-smoke: ## End-to-end Phase 17a check: Keycloak tokens accepted by every service on the request path; refusals
	@./scripts/smoke-phase17a.sh

# ---------- Console (Phase 17b) - React + TypeScript + Vite in frontend/, Node >= 20 ----------
openapi: ## Regenerate the api contract (frontend/openapi.json) and the console's TypeScript types from it
	uv run python scripts/export_openapi.py
	cd frontend && npm run -s gen:api

ui-install: ## Install the console's pinned dependencies (frontend/package-lock.json)
	cd frontend && npm ci --no-audit --no-fund

ui-dev: ## Console on http://localhost:5173 (proxies /api to :8000; sign-in through Keycloak - make keycloak-up)
	cd frontend && npm run dev

ui-check: ## Console: typecheck + component tests + generated types match openapi.json
	cd frontend && npm run -s typecheck && npm run -s test && npm run -s check:api

ui-browser-test: ## Console e2e in a real browser (real Keycloak + api; first run downloads Chromium). AEOI_UI_DECIDE=1 adds approve-and-run
	cd frontend && npx playwright install chromium && npm run -s e2e

ui-demo: ## A PENDING rollback request (throwaway service) to approve IN THE CONSOLE; prints what to do
	@./scripts/ui-demo-request.sh

ui-demo-clean: ## Remove the throwaway deploy rows planted by ui-demo / approval-smoke
	@uv run --quiet python scripts/phase16_fixture.py clean

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
