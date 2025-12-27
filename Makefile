.PHONY: lint lint-ruff lint-pyright ruff-fix fmt test reqs dev-reqs \
        build up down restart logs ps status \
        profiles active create use switch edit \
        port doctor

PYTHON ?= python
IMAGE ?= llm-proxy:dev

# CLI entrypoint
CLI ?= ./cli/llm-proxy

# Profile management
PROFILE ?= default
PORT ?= 8002

# Detect profile dir (XDG). Keep it simple and modern.
PROFILE_DIR := $(shell if [ -n "$$XDG_CONFIG_HOME" ]; then echo "$$XDG_CONFIG_HOME/llm-proxy/profiles"; else echo "$$HOME/.config/llm-proxy/profiles"; fi)
COMPOSE_ENV := $(PROFILE_DIR)/$(PROFILE).env
RUNTIME_ENV := $(PROFILE_DIR)/$(PROFILE).llm.env


# -----------------------
# Python quality targets
# -----------------------
lint: lint-ruff lint-pyright

lint-ruff:
	$(PYTHON) -m ruff check src tests

lint-pyright:
	pyright

ruff-fix:
	$(PYTHON) -m ruff check src tests --fix

fmt:
	$(PYTHON) -m ruff format src tests

test:
	pytest -q

reqs:
	pip install -r requirements.txt

dev-reqs:
	pip install -r requirements-dev.txt


# -----------------------
# Docker image
# -----------------------
build:
	docker build -t $(IMAGE) .


# -----------------------
# CLI-driven lifecycle
# -----------------------

# List profiles known to CLI
profiles:
	$(CLI) list

# Show active profile (if CLI supports it)
active:
	$(CLI) active || true

# Create profile if missing (non-interactive)
create:
	@mkdir -p "$(PROFILE_DIR)"
	@if [ -f "$(COMPOSE_ENV)" ] || [ -f "$(RUNTIME_ENV)" ]; then \
		echo "Profile '$(PROFILE)' already exists in $(PROFILE_DIR)"; \
	else \
		$(CLI) create "$(PROFILE)"; \
	fi

# Switch active profile
use:
	$(CLI) use "$(PROFILE)"

# Convenience: switch profile in one command
switch: create use

# Open editor for profile (interactive)
edit:
	$(CLI) edit "$(PROFILE)"

# Ensure PORT is written as a single source of truth into profile env files.
# This edits both "<profile>.env" and "<profile>.llm.env" if they exist.
port: create
	@echo "Setting LLM_PORT=$(PORT) for profile '$(PROFILE)'"
	@if [ -f "$(COMPOSE_ENV)" ]; then \
		if grep -qE '^LLM_PORT=' "$(COMPOSE_ENV)"; then \
			sed -i.bak -E 's/^LLM_PORT=.*/LLM_PORT=$(PORT)/' "$(COMPOSE_ENV)"; \
		else \
			printf "\nLLM_PORT=$(PORT)\n" >> "$(COMPOSE_ENV)"; \
		fi; \
		rm -f "$(COMPOSE_ENV).bak"; \
	fi
	@if [ -f "$(RUNTIME_ENV)" ]; then \
		if grep -qE '^LLM_PORT=' "$(RUNTIME_ENV)"; then \
			sed -i.bak -E 's/^LLM_PORT=.*/LLM_PORT=$(PORT)/' "$(RUNTIME_ENV)"; \
		else \
			printf "\nLLM_PORT=$(PORT)\n" >> "$(RUNTIME_ENV)"; \
		fi; \
		rm -f "$(RUNTIME_ENV).bak"; \
	fi
	@echo "Done. Compose env: $(COMPOSE_ENV)"
	@echo "Done. Runtime env: $(RUNTIME_ENV)"

# Start using CLI (create+use+up)
up: create use
	$(CLI) up "$(PROFILE)"

# Stop using CLI
down:
	$(CLI) down "$(PROFILE)" || $(CLI) down || true

restart: down up

# Optional helpers (may depend on your CLI implementation; harmless if not supported)
logs:
	$(CLI) logs "$(PROFILE)" || docker ps --format "table {{.Names}}\t{{.Ports}}" || true

ps:
	docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"

status:
	@echo "PROFILE=$(PROFILE)"
	@echo "PROFILE_DIR=$(PROFILE_DIR)"
	@echo "COMPOSE_ENV=$(COMPOSE_ENV)"
	@echo "RUNTIME_ENV=$(RUNTIME_ENV)"
	@echo
	@$(CLI) active || true
	@$(CLI) list || true
	@echo
	@docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}" || true

# Quick diagnostics: check port availability + show containers using it
doctor:
	@echo "Checking port $(PORT)..."
	@docker ps --format "{{.Names}} {{.Ports}}" | grep -E "0.0.0.0:$(PORT)->|:::$(PORT)->" || true
	@echo
	@echo "If something is holding the port, stop it (docker stop <name>) or run: make down PROFILE=$(PROFILE)"
