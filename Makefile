PYTHON_VERSION := 3.12.3
VENV_NAME := connected-diagnostics

# Linux needs sudo for docker; macOS doesn't
ifeq ($(shell uname),Linux)
  DOCKER := sudo docker
else
  DOCKER := docker
endif

.PHONY: setup setup-deps setup-pyenv setup-venv install db-up db-down migrate migration ingest chat clean

## setup: Full setup — system deps, pyenv, virtualenv, dependencies, done.
setup: setup-deps setup-pyenv setup-venv install
	@echo ""
	@echo "Setup complete! Run: pyenv activate $(VENV_NAME)"
	@echo "Then copy .env.example to .env and add your API keys."

## setup-deps: Install system build dependencies for pyenv Python compilation
setup-deps:
ifeq ($(shell uname),Darwin)
	brew install openssl readline sqlite3 xz zlib tcl-tk
else ifeq ($(shell command -v apt 2>/dev/null),)
	$(error Unsupported system — install pyenv build deps manually: https://github.com/pyenv/pyenv/wiki#suggested-build-environment)
else
	sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
		libreadline-dev libsqlite3-dev libncursesw5-dev xz-utils tk-dev \
		libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev
endif

## setup-pyenv: Install pyenv if missing, then install Python $(PYTHON_VERSION)
setup-pyenv:
	@if ! command -v pyenv >/dev/null 2>&1; then \
		echo "pyenv not found. Installing..."; \
		curl -fsSL https://pyenv.run | bash; \
		echo ""; \
		echo "Add these to your ~/.bashrc or ~/.zshrc:"; \
		echo '  export PYENV_ROOT="$$HOME/.pyenv"'; \
		echo '  export PATH="$$PYENV_ROOT/bin:$$PATH"'; \
		echo '  eval "$$(pyenv init -)"'; \
		echo '  eval "$$(pyenv virtualenv-init -)"'; \
		echo ""; \
		echo "Then restart your shell and re-run: make setup"; \
		exit 1; \
	fi
	@echo "pyenv found: $$(pyenv --version)"
	@if ! pyenv versions --bare | grep -q "^$(PYTHON_VERSION)$$"; then \
		echo "Installing Python $(PYTHON_VERSION)..."; \
		pyenv install $(PYTHON_VERSION); \
	else \
		echo "Python $(PYTHON_VERSION) already installed."; \
	fi

## setup-venv: Create pyenv virtualenv and set as local
setup-venv:
	@if ! pyenv versions --bare | grep -q "^$(VENV_NAME)$$"; then \
		echo "Creating virtualenv $(VENV_NAME)..."; \
		pyenv virtualenv $(PYTHON_VERSION) $(VENV_NAME); \
	else \
		echo "Virtualenv $(VENV_NAME) already exists."; \
	fi
	@pyenv local $(VENV_NAME)
	@echo "Local pyenv set to $(VENV_NAME)"

## install: Install Poetry and project dependencies into pyenv virtualenv
PYENV_VENV := $(HOME)/.pyenv/versions/$(VENV_NAME)
PYENV_BIN := $(PYENV_VENV)/bin
PYENV_PIP := $(PYENV_BIN)/pip
POETRY := $(PYENV_BIN)/poetry
PYTHON := $(PYENV_BIN)/python
install:
	@if [ ! -x "$(POETRY)" ]; then \
		echo "Installing Poetry into virtualenv..."; \
		$(PYENV_PIP) install poetry; \
	fi
	VIRTUAL_ENV=$(PYENV_VENV) $(POETRY) install

## db-up: Start Neo4j and PostgreSQL via Docker Compose, then run migrations
db-up:
	$(DOCKER) compose up -d
	@echo "Waiting for databases to be ready..."
	@sleep 5
	$(MAKE) migrate
	@echo "Neo4j:     http://localhost:7474 (neo4j/password)"
	@echo "PostgreSQL: localhost:5432 (postgres/password)"

## db-down: Stop databases
db-down:
	$(DOCKER) compose down

ALEMBIC := $(PYENV_BIN)/alembic

## migrate: Run Alembic migrations to head
migrate:
	$(ALEMBIC) upgrade head

## migration: Autogenerate a new migration (usage: make migration msg="description")
migration:
	$(ALEMBIC) revision --autogenerate -m "$(msg)"

## ingest: Ingest the Lexus GX460 service manual
ingest:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021

## ingest-dry: Dry run — parse and chunk only, no LLM calls
ingest-dry:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--dry-run

## ingest-sample: Ingest only first 50 pages (cheaper test run)
ingest-sample:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--end-page 50

## embed-sample: Embed only first 50 pages (skip LLM extraction, use after failed run)
embed-sample:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--end-page 50 --embed-only

## ingest-diag: Ingest pages 2400-2500 (engine DTC diagnostic procedures)
ingest-diag:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--start-page 2400 --end-page 2500

## ingest-diag-ocr: Ingest pages 2400-2500 with OCR for image pages
ingest-diag-ocr:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--start-page 2400 --end-page 2500 \
		--ocr

## ingest-diag-reextract: Re-run LLM extraction on existing chunks (pages 2400-2500)
ingest-diag-reextract:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--start-page 2400 --end-page 2500 \
		--reextract

## ingest-diag-dry: Dry run pages 2400-2500
ingest-diag-dry:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--start-page 2400 --end-page 2500 \
		--dry-run

## extract-missing: Extract only chunks that don't have graph nodes yet (non-destructive)
extract-missing:
	$(PYTHON) -m backend.cli.ingest \
		--pdf ~/Downloads/"2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf" \
		--make Lexus --model GX460 \
		--year-start 2016 --year-end 2021 \
		--extract-missing

## qa: Analyze graph quality for pages 2400-2500
qa:
	$(PYTHON) -m backend.cli.qa analyze --start-page 2400 --end-page 2500

## qa-history: Show QA run history for pages 2400-2500
qa-history:
	$(PYTHON) -m backend.cli.qa history --start-page 2400 --end-page 2500

## qa-compare: Compare last two QA runs for pages 2400-2500
qa-compare:
	$(PYTHON) -m backend.cli.qa compare --start-page 2400 --end-page 2500

## chat: Start diagnostic chat
chat:
	$(PYTHON) -m backend.cli.chat --vehicle "2017 Lexus GX460"

## clean: Remove virtualenv and local pyenv setting
clean:
	-pyenv virtualenv-delete -f $(VENV_NAME)
	-rm -f .python-version
