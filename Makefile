.DEFAULT_GOAL := all

CI_PYTHON := 3.13
PYTHON ?= python
VENV_BIN := .venv/bin
ifeq ($(OS),Windows_NT)
VENV_BIN := .venv/Scripts
endif
DEV_PYTHON := $(VENV_BIN)/python
export PATH := $(abspath $(VENV_BIN)):$(PATH)
export UV_SYSTEM_PYTHON := 0
UV_VERSION := 0.13.6
CI_BUILD_REQUIREMENTS = $(DEV_PYTHON) -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"]))'
CI_COMPILE = uv pip compile pyproject.toml - --universal --python-version $(CI_PYTHON) --no-python-downloads --no-sources --no-build --generate-hashes --no-annotate --custom-compile-command 'make lock-update'

# Every file git tracks or would add, except paths git marks `private`.
git_files = $(wildcard $(shell git -c core.quotePath=false ls-files --cached --others --exclude-standard -- $(1) ':(exclude,attr:private)'))
MARKDOWN_FILES = $(call git_files,'*.md')
PYTHON_FILES = $(call git_files,'*.py' '*.pyi')
WORKFLOW_FILES := $(wildcard .github/workflows/*.yml)

.PHONY: all check dead-code-check env-check file-check format format-check install lock-check lock-update manifest-check quality reinstall setup test type-check uv-check venv-check whitespace-check workflow-check

uv-check:
	@test "$$(uv --version | cut -d ' ' -f 2)" = "$(UV_VERSION)" || \
		{ echo 'Use uv $(UV_VERSION), matching CI.' >&2; exit 1; }

setup: uv-check
	@test -d .venv || uv venv --python "$(PYTHON)" --no-python-downloads .venv
	rm -f .venv/.requirements-ci.txt .venv/.pyproject.toml
	uv pip sync --python "$(DEV_PYTHON)" --no-python-downloads --require-hashes --build-constraints requirements-ci.txt requirements-ci.txt
	uv pip install --python "$(DEV_PYTHON)" --no-python-downloads --no-deps --no-build-isolation -e .
	cp requirements-ci.txt .venv/.requirements-ci.txt
	cp pyproject.toml .venv/.pyproject.toml

venv-check:
	@test -x "$(DEV_PYTHON)" || { echo 'Run make setup before project commands.' >&2; exit 1; }

env-check: venv-check
	@cmp -s requirements-ci.txt .venv/.requirements-ci.txt && \
		cmp -s pyproject.toml .venv/.pyproject.toml || \
		{ echo 'Project setup is incomplete or outdated; run make setup.' >&2; exit 1; }

# A tool given no files, or a folder, searches folders itself; stop before that can happen.
file-check:
	@test -n "$(PYTHON_FILES)" && test -n "$(MARKDOWN_FILES)" || \
		{ echo 'git listed no files to check; run make from a git checkout.' >&2; exit 1; }
	@git ls-files -z --cached --others --exclude-standard -- '*.py' '*.pyi' '*.md' ':(exclude,attr:private)' | \
		$(DEV_PYTHON) -c 'import re, sys; sys.exit(re.search(rb"[\s\x01-\x1f\x7f\"\\\[\]*?]", sys.stdin.buffer.read()) is not None)' || \
		{ echo 'Rename Python and Markdown files whose names contain whitespace, control characters, quotes, backslashes, or glob characters.' >&2; exit 1; }

install:
	uv tool install .

reinstall:
	uv tool install --reinstall .

format: env-check file-check
	$(DEV_PYTHON) -m ruff format $(PYTHON_FILES)
	$(DEV_PYTHON) -m ruff check --fix $(PYTHON_FILES) pyproject.toml
	biome format --write .
	$(DEV_PYTHON) -m mdformat $(MARKDOWN_FILES)
	$(VENV_BIN)/pyproject-fmt pyproject.toml

format-check: env-check file-check
	$(DEV_PYTHON) -m ruff format --check $(PYTHON_FILES)
	$(DEV_PYTHON) -m ruff check $(PYTHON_FILES) pyproject.toml
	biome format .
	$(DEV_PYTHON) -m mdformat --check $(MARKDOWN_FILES)
	$(VENV_BIN)/pyproject-fmt --check pyproject.toml

lock-check: env-check uv-check
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	cp requirements-ci.txt "$$tmp/requirements-ci.txt" && \
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group dev --group ci-build --quiet --output-file "$$tmp/requirements-ci.txt" && \
	diff -u requirements-ci.txt "$$tmp/requirements-ci.txt" && \
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group test --constraint "$$tmp/requirements-ci.txt" --quiet --output-file "$$tmp/requirements-test.txt" && \
	diff -u requirements-test.txt "$$tmp/requirements-test.txt"

lock-update: venv-check uv-check
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group dev --group ci-build --output-file requirements-ci.txt $(LOCK_FLAGS)
	$(CI_BUILD_REQUIREMENTS) | $(CI_COMPILE) --group test --constraint requirements-ci.txt --output-file requirements-test.txt

type-check: env-check file-check
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonplatform Linux --pythonpath "$(DEV_PYTHON)" $(PYTHON_FILES)
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonversion 3.15 --pythonplatform Linux --pythonpath "$(DEV_PYTHON)" $(PYTHON_FILES)
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonplatform Darwin --pythonpath "$(DEV_PYTHON)" $(PYTHON_FILES)
	$(DEV_PYTHON) -m pyright --project pyproject.toml --pythonplatform Windows --pythonpath "$(DEV_PYTHON)" $(PYTHON_FILES)

test: env-check
	$(DEV_PYTHON) -m pytest -q $(TEST_FLAGS)

dead-code-check: env-check file-check
	$(DEV_PYTHON) -m vulture $(PYTHON_FILES)

manifest-check: env-check
	$(DEV_PYTHON) -m pre_commit validate-manifest .pre-commit-hooks.yaml

whitespace-check:
	git diff --check "$$(git hash-object -t tree /dev/null)" -- . ':(exclude,attr:private)'

workflow-check: env-check
	$(VENV_BIN)/zizmor --offline --no-progress --quiet $(WORKFLOW_FILES)

quality: format-check lock-check type-check dead-code-check manifest-check whitespace-check workflow-check

check: quality test

all: check
