.PHONY: install install-cli dev test test-cov lint format typecheck check clean build all build-frontend dev-frontend test-frontend changelog-draft changelog-build

PYTEST_WORKERS ?= 4
PYTEST_PARALLEL_ARGS = -n $(PYTEST_WORKERS) --dist loadfile
PYTEST_DEFAULT_MARKERS = not real_api and not install_scripts and not performance

# Default target
all: check test

# Install the package
install:
	uv sync

# Install as a global CLI tool
install-cli:
	uv tool install --editable .

# Install with dev dependencies
dev:
	uv sync --group dev

# Run the isolated test suite in parallel. Set PYTEST_WORKERS=0 for a serial run.
test:
	uv run pytest $(PYTEST_PARALLEL_ARGS) -m "$(PYTEST_DEFAULT_MARKERS)"

# Run install-script integration tests (slow; builds wheels, runs install.ps1/install.sh)
test-install-scripts:
	uv run pytest -n 0 -m install_scripts -v

# Run the isolated test suite in parallel with coverage.
test-cov:
	uv run pytest $(PYTEST_PARALLEL_ARGS) -m "$(PYTEST_DEFAULT_MARKERS)" --cov=conductor --cov-report=term-missing

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

# Preview unreleased changelog notes compiled from changelog.d/ fragments
changelog-draft:
	$(if $(strip $(VERSION)),,$(error VERSION is required (e.g. make changelog-draft VERSION=0.1.38)))
	uvx --from towncrier==25.8.0 towncrier build --draft --version $(VERSION)

# Compile changelog fragments into CHANGELOG.md and delete consumed fragment files.
# Note: towncrier removes consumed fragments (via git rm or file deletion); run only in a release-prep PR.
changelog-build:
	$(if $(strip $(VERSION)),,$(error VERSION is required (e.g. make changelog-build VERSION=0.1.38)))
	uvx --from towncrier==25.8.0 towncrier build --version $(VERSION) --yes

# Build frontend dashboard (output to src/conductor/web/static/)
build-frontend:
	cd src/conductor/web/frontend && npm install && npm run build

# Run frontend dev server (with proxy to FastAPI backend)
dev-frontend:
	cd src/conductor/web/frontend && npm run dev

# Run frontend unit tests (Vitest)
test-frontend:
	cd src/conductor/web/frontend && npm install && npm run test
