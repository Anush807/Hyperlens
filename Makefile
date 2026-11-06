# HVOps developer entrypoints. Everything runs through compose so the host only
# needs docker + make.
SHELL := /bin/bash
COMPOSE := docker compose
API := $(COMPOSE) exec -T api

.PHONY: help up down logs ps seed train evaluate test test-api test-collector lint fmt types reset

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the stack (add PROFILE=llm for the assistant)
	@test -f .env || cp .env.example .env
	$(COMPOSE) $(if $(PROFILE),--profile $(PROFILE),) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

reset: ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

logs: ## Tail logs (SVC=api to narrow)
	$(COMPOSE) logs -f $(SVC)

ps: ## Show container status
	$(COMPOSE) ps

seed: ## Backfill DAYS (default 180) of synthetic metric history, then roll it up
	$(COMPOSE) exec -T collector python -m synthetic.generate_history \
		--days $(or $(DAYS),180) --interval $(or $(INTERVAL),300)
	$(API) python -m ml.rollup --days $(or $(DAYS),180)

rollup: ## Re-aggregate metrics_daily from raw points
	$(API) python -m ml.rollup --days $(or $(DAYS),180)

train: ## Train anomaly + forecast models and generate recommendations
	$(API) python -m ml.train anomaly --days 30
	$(API) python -m ml.train forecast --days 180
	$(API) python -m ml.run_all

evaluate: ## Score the models against ground truth into docs/ml-evaluation.md
	$(API) python -m ml.evaluate --out docs/ml-evaluation.md

assistant-eval: ## Score assistant tool selection against tests/assistant_eval.yaml
	$(API) python -m tests.run_assistant_eval

test: test-api test-collector test-frontend ## Run every test suite

test-api:
	$(COMPOSE) run --rm --no-deps api pytest -q

test-collector:
	$(COMPOSE) run --rm --no-deps collector pytest -q

test-frontend:
	cd frontend && npm run test -- --run

# The images do not mount the source, so the linters have to be pointed at the
# working tree explicitly - otherwise `make fmt` edits a layer that is thrown
# away and reports success.
API_SRC := -v "$(CURDIR)/services/api:/src" -w /src
COL_SRC := -v "$(CURDIR)/services:/src" -w /src/collector
RUN_API := docker run --rm $(API_SRC) hvops-api
RUN_COL := docker run --rm $(COL_SRC) hvops-collector

lint: ## ruff + mypy + eslint + tsc
	$(RUN_API) bash -lc "ruff check . && mypy app ml"
	$(RUN_COL) bash -lc "ruff check . && mypy app"
	cd frontend && npm run lint && npm run typecheck

fmt: ## Format Python and TypeScript
	$(RUN_API) ruff format .
	$(RUN_API) ruff check --fix .
	$(RUN_COL) ruff format .
	$(RUN_COL) ruff check --fix .
	cd frontend && npm run format

types: ## Regenerate frontend API types from the live OpenAPI schema
	cd frontend && npm run gen:types
