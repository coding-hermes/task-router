#!/usr/bin/env bash
# router-data-quality.sh — deterministic pipeline step for the
# model-registry-data-quality cron (Bane 2026-08-27).
#
# Runs: models.dev catalog sync -> normalized (subscription-aware) pricing ->
# gap report (JSON for the research agent). The agent then researches
# benchmarks/sentiment, writes evidence rows, re-seeds, exports and commits.
set -uo pipefail
cd /home/kara/task-router || exit 1
PY="${PYTHON:-$HOME/.hermes/venvs/board/bin/python3}"

echo "== models.dev sync =="
"$PY" scripts/router_modelsdev.py sync 2>&1 | tail -12
echo
echo "== normalized pricing (subscription-aware) =="
"$PY" scripts/router_pricing.py 2>&1 | tail -10
echo
echo "== gap report (JSON) =="
"$PY" scripts/router_gaps.py --json 2>&1 | head -80
