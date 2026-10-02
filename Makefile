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
#   make watchdog  TR-255: check every fleet listener's /health code.stale
#   make restart-router  TR-255: the deploy contract (pull -> sync -> restart -> verify)
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

# TR-255 deploy contract: the serving units exec THIS tree (see
# scripts/systemd/, docs/health-plane.md), and the live installs under
# ~/.hermes/scripts/ are symlinks into it — so a pull alone is not a deploy:
# sync_runtime.sh must run and the units must restart. CANONICAL_TREE is the
# checkout systemd actually runs from; when this Makefile runs inside a
# wt/* worktree the pull must still land in the canonical tree, not here.
CANONICAL_TREE      ?= /home/kara/task-router
ROUTER_UNITS        := task-router-server.service task-router-proxy.service task-router-web.service
# Only the instances that speak /health verify the restart (the :9093 web UI
# serves no /health route today — see router_stale_watchdog.py's docstring).
RESTART_VERIFY_URLS := http://127.0.0.1:9092 http://127.0.0.1:9391

.PHONY: help venv install seed validate status serve watchdog restart-router test guard hooks smoke schemas clean

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
	@$(VENV_PY) scripts/router_seed.py

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

# TR-255: the consumer of /health's code.stale. One line per fleet listener,
# exit 0 all OK / 1 any STALE / 2 any UNREACHABLE — the cron-alert surface.
# Extra args pass through: make watchdog ARGS="--report-only --json"
watchdog:
	@$(PYTHON) scripts/router_stale_watchdog.py $(ARGS)

# TR-255 deploy contract (docs/health-plane.md, proxy-test-plan-2026-09-25):
# deploy == pull the canonical tree + sync_runtime.sh + restart the serving
# units + a /health parity check. Guarded by REAL assertions — a tree state it
# does not understand or a unit that is not systemd-managed fails loudly and
# touches nothing. Operator-level by design: the worker never restarts.
restart-router:
	@test -d "$(CANONICAL_TREE)/.git" || { echo "FATAL: $(CANONICAL_TREE) is not a git checkout — set CANONICAL_TREE=<the tree systemd runs>"; exit 1; }
	@test -f "$(CANONICAL_TREE)/scripts/sync_runtime.sh" || { echo "FATAL: $(CANONICAL_TREE)/scripts/sync_runtime.sh missing"; exit 1; }
	@if systemctl is-active --quiet task-router-server.service || systemctl is-active --quiet task-router-proxy.service || systemctl is-active --quiet task-router-web.service; then \
	  echo "OK      serving units found (systemd --user)"; \
	else \
	  echo "FATAL: task-router-* units are not active systemd --user units on this host — a scripted restart would be guesswork."; \
	  echo "       Operator commands instead:"; \
	  for u in $(ROUTER_UNITS); do echo "         systemctl --user restart $$u"; done; \
	  exit 1; \
	fi
	@echo "== step 1/4: git -C $(CANONICAL_TREE) pull --ff-only (a non-conflicting dirty tree — e.g. the live board — is tolerated; conflicts fail loud)"
	@git -C "$(CANONICAL_TREE)" status --porcelain | sed 's/^/   dirty: /' || true
	@git -C "$(CANONICAL_TREE)" pull --ff-only || { echo "FATAL: pull failed (fix the canonical tree by hand)"; exit 1; }
	@echo "== step 2/4: $(CANONICAL_TREE)/scripts/sync_runtime.sh (a pull alone is not a deploy: live installs are symlinks into the tree, but the copy-list is not)"
	@bash "$(CANONICAL_TREE)/scripts/sync_runtime.sh" || { echo "FATAL: sync_runtime.sh failed"; exit 1; }
	@echo "== step 3/4: restarting $(ROUTER_UNITS)"
	@for u in $(ROUTER_UNITS); do \
	  echo "-- systemctl --user restart $$u"; \
	  systemctl --user restart "$$u" || { echo "FATAL: restart $$u failed"; exit 1; }; \
	done
	@echo "== step 4/4: verifying deploy parity via /health (waiting for the units to come back up)"
	@sleep 2; \
	ok=0; tries=0; \
	while [ $$tries -lt 20 ]; do \
	  if $(PYTHON) scripts/router_stale_watchdog.py $(RESTART_VERIFY_URLS) --json >/tmp/tr255_restart_verify.json 2>/tmp/tr255_restart_verify.err; then ok=1; break; fi; \
	  tries=$$((tries + 1)); sleep 2; \
	done; \
	if [ $$ok -eq 1 ]; then \
	  echo "VERIFIED: all health-speaking instances report code.stale=false against the new tree"; \
	  exit 0; \
	fi; \
	echo "FATAL: post-restart parity check did not turn green within ~40s:"; \
	$(PYTHON) scripts/router_stale_watchdog.py $(RESTART_VERIFY_URLS) || true; \
	cat /tmp/tr255_restart_verify.err 2>/dev/null; \
	echo "Operator follow-up: systemctl --user status task-router-server.service task-router-proxy.service"; \
	exit 1

clean:
	@rm -rf $(VENV)
	@echo "removed $(VENV)"
