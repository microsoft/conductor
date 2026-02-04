.PHONY: install install-cli dev test test-cov lint format typecheck check clean build all

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

# Run tests
test:
	uv run pytest

# Run tests with coverage
test-cov:
	uv run pytest --cov=conductor --cov-report=term-missing

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
