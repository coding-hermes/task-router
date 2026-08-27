#!/usr/bin/env bash
# router-data-quality.sh — deterministic pipeline step for the
# model-registry-data-quality cron (Bane 2026-08-27).
#
# Runs: clinepass API sync (catalog + plans + discounts) -> models.dev catalog
# sync -> normalized (subscription-aware) pricing -> gap report (JSON for the
# research agent). The agent then researches benchmarks/sentiment + provider
# rules/billing, writes evidence rows, re-seeds, exports and commits.
set -uo pipefail
cd /home/kara/task-router || exit 1
PY="${PYTHON:-$HOME/.hermes/venvs/board/bin/python3}"

echo "== learning memory (duckbrain task-router ns: doctrine + providers + lessons) =="
"$PY" scripts/router_learn.py dump 2>&1 | grep -v "Cleared stale" | head -60
echo
echo "== clinepass API sync (catalog + plans + discounts) =="
"$PY" scripts/router_clinepass.py sync 2>&1 | tail -6
echo
echo "== plan sweep (disable PAYG-outside-flat-plan lanes) =="
"$PY" scripts/router_plan_sweep.py --apply 2>&1 | tail -3
echo
echo "== models.dev sync =="
"$PY" scripts/router_modelsdev.py sync 2>&1 | tail -12
echo
echo "== normalized pricing (subscription-aware) =="
"$PY" scripts/router_pricing.py 2>&1 | tail -10
echo
echo "== gap report (JSON) =="
"$PY" scripts/router_gaps.py --json 2>&1 | head -80
