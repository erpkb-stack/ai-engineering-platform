# AEOI Platform — targets are added phase by phase. macOS/zsh compatible.
SHELL := /bin/zsh
.DEFAULT_GOAL := help

.PHONY: help doctor
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

doctor: ## Check local macOS prerequisites
	@./scripts/doctor.sh
