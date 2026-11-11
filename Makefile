.PHONY: help install test coverage coverage-html run seed clean

PYTHON ?= python3
PIP ?= pip
PYTEST ?= pytest

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime and development dependencies
	$(PIP) install -r requirements.txt -r requirements-dev.txt

test: ## Run unit and integration tests
	PYTHONPATH=. $(PYTEST) tests -v

coverage: ## Run test suite and enforce 65% coverage quality gate
	PYTHONPATH=. $(PYTEST) tests --cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=65

coverage-html: ## Generate interactive browser coverage report in htmlcov/
	PYTHONPATH=. $(PYTEST) tests --cov=app --cov-report=html
	@echo "Coverage HTML report generated at htmlcov/index.html"

run: ## Run local FastAPI development server
	uvicorn app.main:app --reload --port 8000

seed: ## Seed all 420 apartments into database from CSV
	python -m app.extractor.seed_units

clean: ## Clean cache, coverage, and build artifacts
	rm -rf .pytest_cache htmlcov .coverage coverage.xml
	find . -type d -name "__pycache__" -exec rm -rf {} +
