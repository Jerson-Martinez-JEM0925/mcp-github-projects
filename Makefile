# ── GitHub Project MCP Server — Makefile ─────────────────────────────────────
# All targets execute inside Docker. No host dependencies beyond Docker itself.
#
# Setup:
#   cp .env.example .env && edit .env
#   make build
#   make verify
#
# Quick reference:
#   make build      — Build the MCP Docker image
#   make verify     — Validate auth + scopes + target config
#   make test       — Run unit tests
#   make validate   — Full CI mirror (build + syntax + tests + tools + secrets)
#   make tools      — Count registered tools
#   make shell      — Drop into a shell inside the MCP container
#   make preflight  — Check host prerequisites
#   make secrets    — Scan for leaked credentials
#   make lint       — Ruff correctness lint
#   make typecheck  — mypy hardened core/client boundary
#   make security   — Bandit + pip-audit
#   make lock       — Regenerate hash-pinned dependency locks
#   make clean      — Remove built images
# ─────────────────────────────────────────────────────────────────────────────

IMAGE       := mcp-github-projects:latest
TEST_IMAGE  := mcp-github-projects:test
COMPOSE     := docker compose -f compose.yaml
ENV_FILE    := .env
DOCKER_RUN  := docker run --rm --env-file $(ENV_FILE) $(IMAGE)

# ── Default ──────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show available targets
	@echo "═══ MCP Makefile Targets ═══"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Setup: cp .env.example .env → edit token → make build → make verify"

# ── Build ────────────────────────────────────────────────────────────────────
.PHONY: build
build: ## Build the MCP Docker image
	docker build -t $(IMAGE) .

.PHONY: rebuild
rebuild: ## Force rebuild without cache
	docker build -t $(IMAGE) --no-cache .

.PHONY: lock
lock: ## Regenerate hash-pinned runtime/dev dependency locks
	@docker run --rm -u root \
		-v "$(CURDIR):/app" -w /app \
		python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b \
		bash scripts/generate_locks.sh

# ── Verification ─────────────────────────────────────────────────────────────
.PHONY: verify
verify: _check_env ## Validate auth, scopes, and target configuration
	@$(DOCKER_RUN) python3 scripts/verify_setup.py

.PHONY: test
test: _check_env ## Run unit tests inside Docker
	@docker build --build-arg INSTALL_TEST_DEPS=true -t $(TEST_IMAGE) .
	@docker run --rm $(TEST_IMAGE) \
		python3 -m pytest tests/ -v --tb=short

.PHONY: tools
tools: _check_env ## Count registered MCP tools (must be >= 100)
	@$(DOCKER_RUN) python3 scripts/count_tools.py

.PHONY: syntax
syntax: ## Check Python syntax (ast.parse all .py files)
	@$(DOCKER_RUN) python3 scripts/check_syntax.py

.PHONY: lint
lint: ## Run Ruff correctness lint in the test image
	@docker build --build-arg INSTALL_TEST_DEPS=true -t $(TEST_IMAGE) .
	@docker run --rm $(TEST_IMAGE) ruff check --no-cache .

.PHONY: typecheck
typecheck: ## Run mypy on the hardened core/client boundary
	@docker build --build-arg INSTALL_TEST_DEPS=true -t $(TEST_IMAGE) .
	@docker run --rm $(TEST_IMAGE) mypy \
		clients/graphql_client.py clients/rest_client.py clients/resource_cache.py \
		clients/gh_cli_client.py core/config.py core/http_auth.py core/observability.py \
		core/context.py core/metrics.py core/circuit_breaker.py

.PHONY: security
security: ## Run Bandit and pip-audit in the test image
	@docker build --build-arg INSTALL_TEST_DEPS=true -t $(TEST_IMAGE) .
	@docker run --rm $(TEST_IMAGE) bandit -r . -x ./tests,./docs,./profiles -ll -ii
	@docker run --rm $(TEST_IMAGE) pip-audit --progress-spinner off

.PHONY: validate
validate: build syntax test tools secrets lint typecheck security ## Full validation (mirrors CI pipeline)
	@echo ""
	@echo "═══ All checks passed. Safe to push. ═══"

# ── Development ──────────────────────────────────────────────────────────────
.PHONY: shell
shell: _check_env ## Drop into a shell inside the MCP container
	@docker run --rm -it --env-file $(ENV_FILE) $(IMAGE) /bin/bash

.PHONY: run
run: _check_env ## Start MCP server (stdio mode)
	@$(COMPOSE) run --rm mcp

.PHONY: call
call: _check_env ## Call one MCP tool over stdio: make call TOOL=list_labels ARGS='{}'
	@test -n "$(TOOL)" || { echo "❌ Usage: make call TOOL=<tool> [ARGS='{...}'] [FLAGS='--wrap']"; exit 1; }
	@docker run --rm -i --env-file $(ENV_FILE) $(IMAGE) \
		python3 scripts/mcp_call.py $(TOOL) '$(or $(ARGS),{})' $(FLAGS)

# ── Security ─────────────────────────────────────────────────────────────────
.PHONY: secrets
secrets: ## Scan for leaked credentials in source tree
	@docker build -t $(IMAGE) .
	@docker run --rm -u root -v "$(CURDIR):/app" -w /app $(IMAGE) bash scripts/scan_secrets.sh

.PHONY: preflight
preflight: ## Check host prerequisites (Docker, token, image)
	@bash scripts/preflight.sh

# ── Cleanup ──────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove MCP Docker images
	@docker rmi $(IMAGE) 2>/dev/null || true
	@docker rmi $(TEST_IMAGE) 2>/dev/null || true
	@docker rmi github-project-mcp:validate 2>/dev/null || true
	@docker rmi github-project-mcp:ci 2>/dev/null || true
	@echo "✅ Images removed"

# ── Internal ─────────────────────────────────────────────────────────────────
.PHONY: _check_env
_check_env:
	@test -f $(ENV_FILE) || { echo "❌ Missing $(ENV_FILE). Run: cp .env.example .env"; exit 1; }
