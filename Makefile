UV ?= uv

.PHONY: dev lint format test check validate schema build clean

dev:
	$(UV) sync --all-groups

lint:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run ty check src/ tests/

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test:
	$(UV) run pytest

# What CI runs, and what should be green before a commit.
check: lint test validate

validate:
	$(UV) run steward validate

schema:
	$(UV) run steward schema

build:
	$(UV) build

clean:
	rm -rf dist .pytest_cache .ruff_cache .coverage htmlcov
