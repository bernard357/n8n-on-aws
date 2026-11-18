help:
	@echo "Common commands:"
	@echo "$$ make setup     - Check prerequisites, create the venv, install tooling"
	@echo "$$ make lint      - Run Ruff, mypy and Markdown lint checks"
	@echo "$$ make format    - Format Python code with Ruff"
	@echo "$$ make test      - Run the pytest suite"
	@echo "$$ make coverage  - Run tests with coverage"
	@echo "$$ make validate  - Validate every sample configuration"
	@echo "$$ make schema    - Export the JSON Schema of the configuration file"
	@echo "$$ make build     - Build the wheel and sdist with uv"
	@echo "$$ make clean     - Remove build artifacts and caches"

.ONESHELL:
.EXPORT_ALL_VARIABLES:

PYTHON_PATHS := src/ tests/
MARKDOWN_FILES := README.md AGENTS.md CHANGES.md $(shell find docs -name '*.md')
SAMPLES := $(wildcard fixtures/configuration/*.yaml)

setup: check-prerequisites setup-python

check-prerequisites:
	@command -v python3 >/dev/null || { echo "ERROR: python3 not found — install Python 3.12+"; exit 1; }
	@command -v uv >/dev/null || { echo "ERROR: uv not found — see https://docs.astral.sh/uv/"; exit 1; }
	@command -v markdownlint >/dev/null || echo "WARNING: markdownlint not found — npm install -g markdownlint-cli"

setup-python:
	uv venv --clear $(if $(PYTHON_VERSION),--python $(PYTHON_VERSION),)
	uv pip install --python .venv/bin/python -e ".[dev]"

lint: lint-python lint-markdown

lint-python:
	.venv/bin/ruff check ${PYTHON_PATHS}
	.venv/bin/ruff format --check ${PYTHON_PATHS}
	.venv/bin/mypy

lint-markdown:
	markdownlint ${MARKDOWN_FILES}

format:
	.venv/bin/ruff format ${PYTHON_PATHS}
	.venv/bin/ruff check --fix ${PYTHON_PATHS}

test:
	.venv/bin/pytest -q

coverage:
	.venv/bin/pytest --cov=src/n8n_on_aws --cov-branch --cov-report=term-missing

validate:
	@for f in ${SAMPLES}; do .venv/bin/n8n-on-aws validate -f $$f || exit 1; done

schema:
	.venv/bin/n8n-on-aws schema -o schema/n8n-on-aws.v1.json

build:
	uv build

clean:
	rm -rf dist build htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache cdk.out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

.PHONY: help setup check-prerequisites setup-python lint lint-python lint-markdown format test coverage validate schema build clean
