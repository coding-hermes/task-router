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
# TR-202 re-pass (2026-10-07): was `cd /home/kara/task-router` — a second
# checkout or a worktree run silently exercised the MAIN tree (or exited 1).
# Derive the repo from THIS script's location instead.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1
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
echo "== carrier catalog refresh (TR-167: import for EVERY preset in data/catalogs/) =="
# Adds a lane diff per carrier preset (new lanes, price moves, catalog
# removals). --apply FILES the priced lanes; net-new lanes the catalog cannot
# price are HELD and reported loudly for the research phase below (never
# appended as null debt; holds are NOT an error — they exit 0 so the
# pipeline lives and the agent prices them). An ERRORED preset (unreachable
# catalog etc.) exits 1 per TR-024, but this step deliberately continues to
# the gap report: its diff and holds are agent research input, so a dead
# carrier must not kill the run before the agent sees it.
"$PY" scripts/router_catalog_refresh.py --apply || echo "WARN: a carrier catalog errored (continuing — see diff above; research phase handles holds)"
echo
echo "== normalized pricing (subscription-aware) =="
"$PY" scripts/router_pricing.py
echo
echo "== probe 404 scan + auto-fix (health.jsonl -> probe_fixes/probe_gaps) =="
"$PY" scripts/router_probefix.py
echo
echo "== gap report (JSON — full output, never truncated) =="
"$PY" scripts/router_gaps.py --json
