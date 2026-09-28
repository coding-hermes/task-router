# task-router — the front door.
#
# Exists because a hands-on review found the eight setup steps a newcomer needs
# living only in 535 lines of README prose, which is a real adoption cost: the
# reviewer omitted `seed` and got 15 phantom test failures. Every target below
# wraps a command that already exists; nothing here is new machinery.
#
#   make venv      create the local virtualenv
#   make install   install the package into it (editable)
#   make seed      generate registry.json (the one step needing duckdb)
#   make validate  assert the checkout is healthy
#   make status    print machine-readable registry/health/quota state
#   make serve     run the API + UI on 9092 (foreground: Ctrl-C to stop)
#   make test      the full suite
#   make guard     the repo's gitreins guard command (tests + CI gate)
#   make hooks     install .githooks (the pre-push CI gate)
#   make smoke     fresh-checkout smoke: venv -> install -> seed -> status
#   make schemas   validate live state artifacts against schemas/
#
# PYTHON is overridable: make test PYTHON=/usr/bin/python3

PYTHON   ?= $(shell command -v python3.11 || command -v python3)
VENV     ?= .venv
VENV_PY  := $(VENV)/bin/python
ROUTER   := $(VENV)/bin/router
PORT     ?= 9092
HOST     ?= 127.0.0.1

.PHONY: help venv install seed validate status serve test guard hooks smoke schemas clean

help:
	@sed -n 's/^#   //p' Makefile

venv:
	@test -x $(VENV_PY) || $(PYTHON) -m venv $(VENV)
	@$(VENV_PY) -m pip install -q --upgrade pip
	@echo "venv ready: $(VENV_PY)"

install: venv
	@$(VENV_PY) -m pip install -q -e ".[test]"
	@$(VENV_PY) -c "import pytest, duckdb"
	@$(ROUTER) --help >/dev/null && echo "router installed: $(ROUTER)"

seed: install
	@$(ROUTER) seed

validate: install
	@$(ROUTER) validate --json

status: install
	@$(ROUTER) status

serve: install
	@$(VENV_PY) scripts/router_server.py --mode read-only --host $(HOST) --port $(PORT)

test: install
	@$(VENV_PY) -m pytest -q tests/

guard: install
	@bash scripts/gitreins-guard-tests.sh

hooks:
	@bash scripts/install-hooks.sh

smoke:
	@bash scripts/fresh_clone_smoke.sh

schemas: install
	@$(VENV_PY) scripts/schema_check.py --all

clean:
	@rm -rf $(VENV)
	@echo "removed $(VENV)"
