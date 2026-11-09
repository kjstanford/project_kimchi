# One interface for the project. Everything but the lab-PC targets runs in the container, through compose.
-include .env
export

COMPOSE_PROJECT_NAME ?= $(shell basename "$(CURDIR)" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_\n-' '-')
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
export COMPOSE_PROJECT_NAME HOST_UID HOST_GID

COMPOSE := docker compose -f docker/compose.yaml
RUN := $(COMPOSE) run --rm --no-deps app
PORT ?= 8300
# Lab PC only: the qcodes env's python, with its DLL dirs on PATH (see AGENTS.md)
PY ?= python

.DEFAULT_GOAL := help
.PHONY: help build up down verify clean test lint fmt smoke e2e logs shell lock up-live run-cli notify-test plot-idvg

help: ## list the targets
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-z0-9-]+:.*## / {printf "  %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Everyday
build: ## build the image (Python from docker/uv.lock, frontend from docker/package-lock.json)
	$(COMPOSE) build app

up: ## start the app (dry-run only) on http://127.0.0.1:PORT/ (PORT from .env, default 8300)
	$(COMPOSE) up -d --wait app
	@echo "App: http://127.0.0.1:$(PORT)/  (API contract: /docs)"

down: ## stop the app
	$(COMPOSE) down

verify: lint test smoke ## the gate: lint, every test, a smoke run

clean: ## back to a fresh clone: this project's containers and images, test and dry-run output (data*/ and .env kept)
	$(COMPOSE) --profile tools --profile e2e down --rmi all --volumes --remove-orphans
	rm -rf .ignore/web_dryrun .ignore/_dryrun_test .ignore/_webapp_test .ruff_cache

##@ While developing
test: ## all tests in the container (web app + measurement dry-runs, ~6 min); a subset: ARGS="tests/test_api.py"
	$(RUN) pytest $(ARGS)

lint: ## check the code, changing nothing
	$(RUN) sh -c "ruff check src/app tests && ruff format --check src/app tests"

fmt: ## rewrite the code into the standard format
	$(RUN) ruff format src/app tests

smoke: ## start the app, check a real API result, stop it
	$(COMPOSE) up -d --wait app
	$(COMPOSE) exec -T app python tests/smoke.py || { $(COMPOSE) stop app; exit 1; }
	$(COMPOSE) stop app

e2e: ## a user in Chromium previews and dry-runs 3 devices
	$(COMPOSE) up -d --wait app
	$(COMPOSE) --profile e2e run --rm e2e || { $(COMPOSE) stop app; exit 1; }
	$(COMPOSE) stop app

logs: ## follow the app's output
	$(COMPOSE) logs -f app

plot-idvg: ## every device's Id-Vg on one figure, |Id|*L/W coloured by L, e.g. ARGS="--sample wd_bobafet_6802 --out .pptx_figs/wd_bobafet"
	$(RUN) python -B -m measure.plot_idvg_all $(ARGS)

shell: ## a shell inside the container
	$(COMPOSE) run --rm --no-deps app bash

##@ Only when dependencies change
lock: ## record exact versions in docker/uv.lock and docker/package-lock.json (commit each with its manifest)
	$(COMPOSE) --profile tools run --rm --build lock-py
	$(COMPOSE) --profile tools run --rm --build lock-js

##@ Lab PC only (native, in the qcodes env: the only way to reach GPIB)
up-live: ## the app with LIVE runs allowed, on 127.0.0.1 (make up-live PY=".../envs/qcodes/python.exe")
	PYTHONPATH=src "$(PY)" -B -m app --allow-live --port $(PORT)

run-cli: ## the measurement CLI, e.g. make run-cli PY=... ARGS="--config configs/lc500_idvg.json --limit 2"
	PYTHONPATH=src "$(PY)" -B -m measure.run_map $(ARGS)

notify-test: ## send one test email with the NOTIFY_* settings in .env (live runs email when they end)
	PYTHONPATH=src "$(PY)" -B -m measure.notify --test
