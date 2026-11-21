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
DIRS ?= data data_k4200 data_k4200_stress
# Lab PC only: the qcodes env's python, with its DLL dirs on PATH (see AGENTS.md)
PY ?= python
# Lab PC only: the gds env's python (KLayout), for the layout CSV scripts
GDS_PY ?= python
# reviewer-check: the repository and branch a reviewer clones (default: this one's remote and current branch)
REPO ?= $(shell git remote get-url origin)
BRANCH ?= $(shell git rev-parse --abbrev-ref HEAD)
REVIEW_DIR := .ignore/reviewer_clone

.DEFAULT_GOAL := help
.PHONY: help build up down verify clean test lint fmt smoke e2e logs shell lock up-live run-cli notify-test plot-idvg box-idvg plot-stress plot-gc-ret layout-localbg-v5 sample-devices live-stats dry-run reviewer-check build-gh pr pr-status ruleset-status

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
	rm -rf .ignore/web_dryrun .ignore/cli_dryrun .ignore/_dryrun_test .ignore/_webapp_test .ruff_cache

dry-run: ## the CLI on the fake instruments, no lab needed; output to .ignore/cli_dryrun/, e.g. ARGS="--config configs/lc500_idvg.json --limit 3"
	@case " $(ARGS) " in *" --live "*|*--data-dir*) echo "dry-run takes neither --live nor --data-dir (live runs: make run-cli on the lab PC)"; exit 2;; esac
	$(RUN) python -B -m measure.run_map --data-dir .ignore/cli_dryrun $(ARGS)

reviewer-check: ## a reviewer's path, no lab: fresh clone of the pushed BRANCH, .env from .env.example, build, verify, up, dry-run, clean
	rm -rf $(REVIEW_DIR)
	git clone --quiet --branch $(BRANCH) --single-branch $(REPO) $(REVIEW_DIR)
	cp $(REVIEW_DIR)/.env.example $(REVIEW_DIR)/.env
	# the clone gets its own compose project and none of this checkout's .env settings (mail credentials, PORT)
	cd $(REVIEW_DIR) && env -u COMPOSE_PROJECT_NAME -u PORT -u GH_TOKEN -u NOTIFY_SMTP_USER -u NOTIFY_SMTP_PASSWORD -u NOTIFY_EMAIL_TO -u NOTIFY_EMAIL_FROM sh -c '\
	  git log -1 --format="reviewer-check at %h (%s)" && \
	  "$(MAKE)" build && "$(MAKE)" verify && "$(MAKE)" up && "$(MAKE)" down && \
	  "$(MAKE)" dry-run ARGS="--config configs/lc500_idvg.json --limit 3" && \
	  "$(MAKE)" clean'
	rm -rf $(REVIEW_DIR)

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

box-idvg: ## box plots of VT, muFE, SS per L, e.g. ARGS="--samples wd_bobafet_6802 wd_bobafet_6916 --out .pptx_figs/wd_bobafet"
	$(RUN) python -B -m measure.idvg_metrics_box $(ARGS)

plot-stress: ## every PMU stress run's dVt (stress + recovery) on one figure, e.g. ARGS="--out .pptx_figs/wd_bobafet"
	$(RUN) python -B -m measure.plot_stress_all $(ARGS)

plot-gc-ret: ## every 4200A gain-cell retention run (PMU + SMU) on one figure, e.g. ARGS="--out .pptx_figs/wd_bobafet"
	$(RUN) python -B -m measure.plot_gc_ret_all $(ARGS)

live-stats: ## live runs, active hours (waits at the confirmations left out), devices and emails; read-only, e.g. DIRS="data"
	$(RUN) python -B -m measure.live_stats $(DIRS)

sample-devices: ## a random device subset, every site in N blocks, e.g. ARGS="--config ... --per-site 3 --include TR-R01C14 --out configs/..._devices.csv"
	$(RUN) python -B -m measure.sample_devices $(ARGS)

shell: ## a shell inside the container
	$(COMPOSE) run --rm --no-deps app bash

##@ Only when dependencies change
lock: ## record exact versions in docker/uv.lock and docker/package-lock.json (commit each with its manifest)
	$(COMPOSE) --profile tools run --rm --build lock-py
	$(COMPOSE) --profile tools run --rm --build lock-js

##@ Only when publishing (GitHub CLI in its own container, GH_TOKEN from .env; push the branch first with git)
GH := $(COMPOSE) --profile tools run --rm gh
TITLE ?=
BODY ?= .ignore/pr.md

build-gh: ## build the GitHub CLI image (once, and after a GH_VERSION bump in docker/Dockerfile)
	$(COMPOSE) --profile tools build gh

pr: ## open a pull request BRANCH -> main (never merges), e.g. make pr TITLE="..." BODY=.ignore/pr.md
	@test "$(BRANCH)" != main || { echo "refusing: a pull request comes from a feature branch, not main"; exit 2; }
	@test -n "$(TITLE)" || { echo 'refusing: TITLE="..." is required'; exit 2; }
	@test -f "$(BODY)" || { echo "refusing: BODY file $(BODY) not found (a path inside the repository)"; exit 2; }
	@test -n "$$GH_TOKEN" || { echo "refusing: GH_TOKEN is not set in .env (see .env.example)"; exit 2; }
	$(GH) pr create --base main --head "$(BRANCH)" --title "$(TITLE)" --body-file "$(BODY)"

pr-status: ## the pull request of BRANCH: title, state, link and description
	@test -n "$$GH_TOKEN" || { echo "refusing: GH_TOKEN is not set in .env (see .env.example)"; exit 2; }
	$(GH) pr view "$(BRANCH)" --json number,title,state,isDraft,mergeable,reviewDecision,url --jq \
	  '"#\(.number) \(.title)\nstate: \(.state)\(if .isDraft then " (draft)" else "" end), mergeable: \(.mergeable), review: \(if (.reviewDecision // "") == "" then "none yet" else .reviewDecision end)\n\(.url)"'

ruleset-status: ## the rules GitHub enforces on main now (read-only; the ruleset is imported by hand, docs/0006)
	@test -n "$$GH_TOKEN" || { echo "refusing: GH_TOKEN is not set in .env (see .env.example)"; exit 2; }
	$(GH) api 'repos/{owner}/{repo}/rules/branches/main' --jq \
	  'if length == 0 then "no rules on main: import .github/rulesets/protect-main.json (docs/0006)" else .[] | "\(.type)\t(\(.ruleset_source_type) ruleset \(.ruleset_id))" end'

##@ Lab PC only (native, in the qcodes env: the only way to reach GPIB)
up-live: ## the app with LIVE runs allowed, on 127.0.0.1 (make up-live PY=".../envs/qcodes/python.exe")
	PYTHONPATH=src "$(PY)" -B -m app --allow-live --port $(PORT)

run-cli: ## the measurement CLI, e.g. make run-cli PY=... ARGS="--config configs/lc500_idvg.json --limit 2"
	PYTHONPATH=src "$(PY)" -B -m measure.run_map $(ARGS)

notify-test: ## send one test email with the NOTIFY_* settings in .env (live runs email when they end)
	PYTHONPATH=src "$(PY)" -B -m measure.notify --test

##@ Lab PC only, layouts (native, in the gds env: KLayout reads the GDS)
layout-localbg-v5: ## device CSVs of the localBG v5 die's TFT_region1 blocks (make layout-localbg-v5 GDS_PY=".../envs/gds/python.exe")
	"$(GDS_PY)" -B layouts/make_localbg_v5_csv.py
