.PHONY: install install-cli install-ts dev test test-cov lint format typecheck check clean build all build-frontend dev-frontend

# Default target
all: check test

# Install the package
install:
	uv sync

# Install as a global CLI tool
install-cli:
	uv tool install --editable .

# Install TypeScript CLI as a global conductor-ts command
install-ts:
	cd conductor-ts && pnpm install && pnpm build
	@echo '#!/usr/bin/env bash' > ~/.local/bin/conductor-ts
	@echo 'exec node "$(CURDIR)/conductor-ts/packages/conductor-cli/dist/index.js" "$$@"' >> ~/.local/bin/conductor-ts
	@chmod +x ~/.local/bin/conductor-ts
	@echo "Installed: $$(which conductor-ts)"

# Install with dev dependencies
dev:
	uv sync --group dev

# Run tests
test:
	uv run pytest -m "not install_scripts"

# Run install-script integration tests (slow; builds wheels, runs install.ps1/install.sh)
test-install-scripts:
	uv run pytest -m install_scripts -v

# Run tests with coverage
test-cov:
	uv run pytest -m "not install_scripts" --cov=conductor --cov-report=term-missing

# Run linter and formatter check
lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

# Format code
format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# Type check
typecheck:
	uv run ty check src

# Run all checks (lint + typecheck)
check: lint typecheck

# Clean build artifacts
clean:
	rm -rf build dist *.egg-info
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# Build package
build: clean
	uv build

# Run a workflow (usage: make run WORKFLOW=path/to/workflow.yaml ARGS='--input question="What is Python?"')
run:
	uv run conductor run $(WORKFLOW) $(ARGS)

# Validate example workflows
validate-examples:
	@for file in examples/*.yaml; do \
		echo "Validating $$file..."; \
		uv run conductor validate "$$file" || exit 1; \
	done

# Build frontend dashboard (output to src/conductor/web/static/)
build-frontend:
	cd src/conductor/web/frontend && npm install && npm run build

# Run frontend dev server (with proxy to FastAPI backend)
dev-frontend:
	cd src/conductor/web/frontend && npm run dev
