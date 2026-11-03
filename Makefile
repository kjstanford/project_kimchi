# One interface for the project. Runs in Git Bash on the lab PC and in the Docker image.
# PY: the Python to use. On the lab PC put the qcodes env's DLL dirs on PATH first (AGENTS.md), then:
#   make test PY="$HOME/anaconda3/envs/qcodes/python.exe"
PY ?= python
PORT ?= 8300
IMAGE ?= wafer-probe-web

.PHONY: help test test-web test-measure e2e serve serve-live docker-build docker-test docker-serve

help:          ## list the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

test: test-web test-measure  ## every Python test, on fake instruments only

test-web:      ## web app: smoke, unit, integration, differential (~1 min)
	$(PY) -B -m webapp.tests.test_webapp

test-measure:  ## the measurement dry-run suite (~5 min)
	$(PY) -B -m measure.tests.test_dryrun

e2e:           ## a user in Chromium runs a 3-device dry-run (needs Node + Playwright)
	node webapp/tests/e2e.mjs $(PY)

serve:         ## dry-run only, http://127.0.0.1:$(PORT)/
	$(PY) -B -m webapp --port $(PORT)

serve-live:    ## LAB PC ONLY: live runs on the real instruments, still gated in the browser
	$(PY) -B -m webapp --port $(PORT) --allow-live

docker-build:  ## image for dry-runs and tests (no GPIB inside Docker)
	docker build -t $(IMAGE) .

docker-test: docker-build  ## all Python tests in the container
	docker run --rm $(IMAGE) make test

docker-serve: docker-build  ## dry-run app from the container on http://127.0.0.1:$(PORT)/
	docker run --rm -p 127.0.0.1:$(PORT):8300 $(IMAGE) python -B -m webapp --host 0.0.0.0 --port 8300
