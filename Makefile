.PHONY: lint fmt test docker-build docker-run

PYTHON ?= python
PORT ?= 8022
IMAGE ?= llm-proxy:dev

lint:
	$(PYTHON) -m ruff check src tests
	pyright

ruff-fix:
	$(PYTHON) -m ruff check src tests --fix

fmt:
	$(PYTHON) -m ruff format src tests

test:
	pytest

build:
	docker build -t $(IMAGE) .

up:
	docker run --rm -p $(PORT):$(PORT) -e LLM_PORT=$(PORT) $(IMAGE)

reqs:
	pip install -r requirements.txt

dev-reqs:
	pip install -r requirements-dev.txt
