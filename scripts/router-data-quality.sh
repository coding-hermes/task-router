#!/usr/bin/env bash
# router-data-quality.sh — deterministic pipeline step for the
# model-registry-data-quality cron (Bane 2026-08-27).
#
# Runs: clinepass API sync (catalog + plans + discounts) -> models.dev catalog
# sync -> normalized (subscription-aware) pricing -> gap report (JSON for the
# research agent). The agent then researches benchmarks/sentiment + provider
# rules/billing, writes evidence rows, re-seeds, exports and commits.
#
# Exit codes (TR-024): non-zero on any required-step failure (abort — a
# pipeline that silently skips a step feeds the agent stale data). The gap
# report's JSON is never truncated (full output; the agent reads the file).
set -euo pipefail
cd /home/kara/task-router || exit 1
PY="${PYTHON:-$HOME/.hermes/venvs/board/bin/python3}"

echo "== learning memory (duckbrain task-router ns: doctrine + providers + lessons) =="
"$PY" scripts/router_learn.py dump 2>&1 | grep -v "Cleared stale" | head -60 || true
echo
echo "== clinepass API sync (catalog + plans + discounts) =="
"$PY" scripts/router_clinepass.py sync
echo
echo "== plan sweep (disable PAYG-outside-flat-plan lanes) =="
"$PY" scripts/router_plan_sweep.py --apply
echo
echo "== models.dev sync =="
"$PY" scripts/router_modelsdev.py sync
echo
echo "== normalized pricing (subscription-aware) =="
"$PY" scripts/router_pricing.py
echo
echo "== probe 404 scan + auto-fix (health.jsonl -> probe_fixes/probe_gaps) =="
"$PY" scripts/router_probefix.py
echo
echo "== gap report (JSON — full output, never truncated) =="
"$PY" scripts/router_gaps.py --json
