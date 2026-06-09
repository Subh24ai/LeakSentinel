# LeakSentinel developer Makefile
# Usage: make <target>

PYTHON ?= python3.13
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate: ## Create the virtualenv
	$(PYTHON) -m venv $(VENV)

.PHONY: install
install: $(VENV)/bin/activate ## Install the package (editable) with dev extras
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

.PHONY: db-up
db-up: ## Start Postgres in the background and wait until healthy
	docker compose up -d postgres
	@echo "Waiting for Postgres to become healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' leaksentinel-postgres 2>/dev/null)" = "healthy" ]; do \
		sleep 1; printf '.'; \
	done; \
	echo " ready."

.PHONY: db-down
db-down: ## Stop Postgres (keeps the data volume)
	docker compose down

.PHONY: db-reset
db-reset: ## Stop Postgres and delete the data volume
	docker compose down -v

.PHONY: db-logs
db-logs: ## Tail Postgres logs
	docker compose logs -f postgres

.PHONY: db-init
db-init: ## Create the schema (tables) in the running Postgres
	$(PY) scripts/init_db.py

.PHONY: db-recreate
db-recreate: ## Drop and recreate all tables (destructive)
	$(PY) scripts/init_db.py --reset

.PHONY: gen-data
gen-data: ## Generate synthetic data + load master tables into Postgres
	$(PY) scripts/generate_synthetic_data.py

.PHONY: ingest
ingest: ## Normalize the 4 insurer CSVs into insurer_commission_feeds
	$(PY) -m leaksentinel.ingestion.loader

.PHONY: reconcile
reconcile: ## Run the reconciliation engine and print the confusion summary
	$(PY) -m leaksentinel.reconciliation.engine

.PHONY: gen-docs
gen-docs: ## Generate synthetic insurance PDF documents (one with a planted mismatch)
	$(PY) scripts/generate_documents.py

.PHONY: extract-doc
extract-doc: ## Real LLM extraction of a generated doc + validation (needs an API key)
	$(PY) scripts/run_extraction.py

.PHONY: run
run: ## Start the FastAPI dev server
	$(VENV)/bin/uvicorn leaksentinel.api.main:app --reload

.PHONY: test
test: ## Run the test suite
	$(VENV)/bin/pytest

.PHONY: lint
lint: ## Lint with ruff
	$(VENV)/bin/ruff check src tests

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
