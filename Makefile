# engram — developer Makefile
# Usage: make <target>

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

# ─── Config ───────────────────────────────────────────────────────────────────
PYTHON        ?= python3
PIP           ?= $(PYTHON) -m pip
UV            := $(shell command -v uv 2>/dev/null)
DOCKER_COMPOSE := $(shell docker compose version &>/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

ENGRAM_DIR    ?= $(HOME)/.engram
PACKAGES       = packages/core packages/mcp-server packages/orchestrator \
                 packages/api packages/learning packages/gateway

# Use uv if available, fall back to pip
ifdef UV
  INSTALLER = uv pip
else
  INSTALLER = $(PIP)
endif

# ─── Colors ───────────────────────────────────────────────────────────────────
CYAN  := \033[0;36m
GREEN := \033[0;32m
BOLD  := \033[1m
NC    := \033[0m

define section
  @echo ""
  @echo -e "$(BOLD)$(CYAN)>>> $1$(NC)"
  @echo ""
endef

# ─── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:  ## Show this help message
	@echo ""
	@echo -e "  $(BOLD)engram developer commands$(NC)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf ""} \
	  /^[a-zA-Z_-]+:.*?##/ { printf "  $(CYAN)%-20s$(NC) %s\n", $$1, $$2 } \
	  /^##@/ { printf "\n  $(BOLD)%s$(NC)\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@echo ""

# ─── Development setup ────────────────────────────────────────────────────────
##@ Development

.PHONY: install-dev
install-dev: ## Install all packages in editable/dev mode
	$(call section,Installing all packages in dev mode)
	@for pkg in $(PACKAGES); do \
	  echo -e "  $(CYAN)--$(NC) Installing $$pkg..."; \
	  $(INSTALLER) install -e "$$pkg[dev]" --quiet 2>/dev/null || \
	  $(INSTALLER) install -e "$$pkg" --quiet; \
	done
	@echo -e "  $(GREEN)[ok]$(NC) All packages installed in dev mode."

.PHONY: install
install: ## Install engram-ai meta-package (production)
	$(call section,Installing engram-ai)
	$(INSTALLER) install -e ".[all]"

.PHONY: dev
dev: ## Start dev stack (Neo4j + Qdrant) — no Python server
	$(call section,Starting dev stack)
	$(DOCKER_COMPOSE) up -d neo4j qdrant
	@echo ""
	@echo -e "  $(GREEN)[ok]$(NC) Dev stack started."
	@echo "  Neo4j browser: http://localhost:7474"
	@echo "  Qdrant:        http://localhost:6333"
	@echo ""
	@echo "  Start the Python server separately:"
	@echo "    engram-server --config engram.yaml"

.PHONY: dev-full
dev-full: ## Start full dev stack including engram server container
	$(call section,Starting full dev stack)
	$(DOCKER_COMPOSE) up -d
	@echo ""
	@echo -e "  $(GREEN)[ok]$(NC) Full stack started."

.PHONY: dev-stop
dev-stop: ## Stop dev Docker stack
	$(DOCKER_COMPOSE) down

.PHONY: dev-reset
dev-reset: ## Stop dev stack and wipe all volumes (CAUTION: deletes data)
	@echo "WARNING: This will delete all Neo4j and Qdrant data. Press Ctrl-C to abort."
	@sleep 3
	$(DOCKER_COMPOSE) down -v
	@echo -e "  $(GREEN)[ok]$(NC) Volumes removed."

# ─── Testing ──────────────────────────────────────────────────────────────────
##@ Testing

.PHONY: test
test: ## Run all tests across all packages
	$(call section,Running tests)
	@for pkg in $(PACKAGES); do \
	  if [ -d "$$pkg/tests" ]; then \
	    echo -e "  $(CYAN)--$(NC) Testing $$pkg..."; \
	    $(PYTHON) -m pytest "$$pkg/tests" -v --tb=short -q 2>&1 | tail -5; \
	  fi; \
	done

.PHONY: test-core
test-core: ## Run only core package tests
	$(PYTHON) -m pytest packages/core/tests -v --tb=short

.PHONY: test-mcp
test-mcp: ## Run only MCP server tests
	$(PYTHON) -m pytest packages/mcp-server/tests -v --tb=short

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(call section,Running tests with coverage)
	$(PYTHON) -m pytest \
	  $(patsubst %, %/tests, $(wildcard $(addsuffix /tests, $(PACKAGES)))) \
	  --cov=engram \
	  --cov-report=term-missing \
	  --cov-report=html:htmlcov \
	  -q

.PHONY: test-watch
test-watch: ## Watch for changes and re-run tests (requires pytest-watch)
	$(PYTHON) -m pytest_watch -- -q --tb=short

# ─── Code quality ─────────────────────────────────────────────────────────────
##@ Code Quality

.PHONY: lint
lint: ruff mypy ## Run all linters (ruff + mypy)

.PHONY: ruff
ruff: ## Run ruff linter
	$(call section,Running ruff)
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format . --check

.PHONY: format
format: ## Auto-format code with ruff
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check . --fix

.PHONY: mypy
mypy: ## Run mypy type checker
	$(call section,Running mypy)
	$(PYTHON) -m mypy \
	  packages/core/engram \
	  packages/mcp-server/engram_mcp \
	  packages/orchestrator/engram_orchestrator \
	  --ignore-missing-imports \
	  --no-error-summary \
	  2>&1 | tail -20

.PHONY: check
check: ruff mypy ## Lint without auto-fix (for CI)

# ─── Build ────────────────────────────────────────────────────────────────────
##@ Build

.PHONY: build
build: ## Build all packages as wheels
	$(call section,Building all packages)
	@for pkg in $(PACKAGES); do \
	  echo -e "  $(CYAN)--$(NC) Building $$pkg..."; \
	  $(PYTHON) -m build "$$pkg" --outdir dist/; \
	done
	@echo ""
	@echo -e "  $(GREEN)[ok]$(NC) Wheels written to dist/"

.PHONY: build-meta
build-meta: ## Build root engram-ai meta-package
	$(PYTHON) -m build --outdir dist/

.PHONY: publish-test
publish-test: build ## Publish to TestPyPI
	$(PYTHON) -m twine upload --repository testpypi dist/*

.PHONY: publish
publish: build ## Publish to PyPI (production)
	$(PYTHON) -m twine upload dist/*

# ─── Docker ───────────────────────────────────────────────────────────────────
##@ Docker

.PHONY: docker-build
docker-build: ## Build the engram Docker image
	$(call section,Building Docker image)
	docker build -t engram:dev -f docker/Dockerfile .

.PHONY: docker-build-nc
docker-build-nc: ## Build Docker image with no cache
	docker build --no-cache -t engram:dev -f docker/Dockerfile .

.PHONY: docker-push
docker-push: ## Push Docker image to registry (set REGISTRY env var)
	docker tag engram:dev $(REGISTRY)/engram:$(shell git rev-parse --short HEAD)
	docker push $(REGISTRY)/engram:$(shell git rev-parse --short HEAD)

.PHONY: docker-logs
docker-logs: ## Tail all Docker container logs
	$(DOCKER_COMPOSE) logs -f

# ─── Config / env ─────────────────────────────────────────────────────────────
##@ Config

.PHONY: setup-env
setup-env: ## Copy .env.example to .env if .env doesn't exist
	@if [ ! -f .env ]; then \
	  cp .env.example .env; \
	  echo -e "  $(GREEN)[ok]$(NC) Created .env from .env.example — edit it with your API keys."; \
	else \
	  echo "  .env already exists — not overwritten."; \
	fi

.PHONY: setup-config
setup-config: ## Copy engram.yaml.example to engram.yaml if not present
	@if [ ! -f engram.yaml ]; then \
	  cp engram.yaml.example engram.yaml; \
	  echo -e "  $(GREEN)[ok]$(NC) Created engram.yaml from example."; \
	else \
	  echo "  engram.yaml already exists — not overwritten."; \
	fi

.PHONY: setup
setup: setup-env setup-config install-dev ## Full dev setup (env + config + install)
	@echo ""
	@echo -e "  $(GREEN)[ok]$(NC) Dev environment ready."
	@echo "  Next: edit .env with your API keys, then: make dev"
	@echo ""

# ─── Cleanup ──────────────────────────────────────────────────────────────────
##@ Cleanup

.PHONY: clean
clean: ## Remove build artifacts, caches, and .pyc files
	$(call section,Cleaning up)
	@find . -type d -name "__pycache__" -not -path "./.git/*" | xargs rm -rf
	@find . -name "*.pyc" -not -path "./.git/*" -delete
	@find . -name "*.pyo" -not -path "./.git/*" -delete
	@find . -name "*.egg-info" -not -path "./.git/*" -type d | xargs rm -rf
	@find . -name ".ruff_cache" -type d | xargs rm -rf
	@find . -name ".mypy_cache" -type d | xargs rm -rf
	@find . -name ".pytest_cache" -type d | xargs rm -rf
	@rm -rf dist/ build/ htmlcov/ .coverage
	@echo -e "  $(GREEN)[ok]$(NC) Clean."

.PHONY: clean-docker
clean-docker: ## Remove dangling Docker images and stopped containers
	docker system prune -f
	docker volume prune -f

# ─── Utilities ────────────────────────────────────────────────────────────────
##@ Utilities

.PHONY: logs
logs: ## Tail engram server logs
	@if [ -f "$(ENGRAM_DIR)/logs/engram.log" ]; then \
	  tail -f "$(ENGRAM_DIR)/logs/engram.log"; \
	else \
	  $(DOCKER_COMPOSE) logs -f engram; \
	fi

.PHONY: shell-neo4j
shell-neo4j: ## Open cypher-shell in Neo4j container
	docker exec -it engram-neo4j cypher-shell -u neo4j -p "$$(grep NEO4J_PASSWORD .env | cut -d= -f2)"

.PHONY: version
version: ## Show installed package versions
	@for pkg in $(PACKAGES); do \
	  name=$$(grep '^name' "$$pkg/pyproject.toml" | head -1 | cut -d'"' -f2); \
	  ver=$$($(PYTHON) -c "import importlib.metadata; print(importlib.metadata.version('$$name'))" 2>/dev/null || echo "not installed"); \
	  printf "  %-30s %s\n" "$$name" "$$ver"; \
	done

.PHONY: update-deps
update-deps: ## Update all dependencies to latest compatible versions
	@for pkg in $(PACKAGES); do \
	  echo -e "  $(CYAN)--$(NC) Updating $$pkg..."; \
	  $(INSTALLER) install -e "$$pkg" --upgrade --quiet; \
	done

.PHONY: security-audit
security-audit: ## Run pip-audit for known vulnerabilities
	$(PYTHON) -m pip_audit \
	  $(patsubst %, -r %/requirements.txt, $(wildcard $(addsuffix /requirements.txt, $(PACKAGES)))) \
	  2>/dev/null || \
	$(PYTHON) -m pip_audit --desc

.PHONY: ci
ci: check test ## Run full CI suite (lint + type check + tests)
	@echo ""
	@echo -e "  $(GREEN)[ok]$(NC) CI passed."
