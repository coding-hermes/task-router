#!/usr/bin/env bash
# sync_runtime.sh — repo/scripts → ~/.hermes/scripts canonical wiring (TR-004)
#
# Repo scripts/ is the single source of truth for the router runtime tools.
# The live installs the scheduler + cron call live at ~/.hermes/scripts/.
# Two topologies exist, by consumer:
#   1. SYMLINKS — router_spawn.py, router_circuit.py, router_quota.py,
#      router_ledger.py, router_seed.py, router_maintain.py. Consumers:
#      scheduler daemon subprocess, foremen, manual CLI calls. No path guard —
#      symlinks exec the canonical file directly.
#   2. BYTE-IDENTICAL COPY — provider_health_probe.py. The Hermes cron runner
#      resolves symlinks and BLOCKS any script whose real path falls outside
#      ~/.hermes/scripts/ ("Blocked: script path resolves outside the scripts
#      directory"), so the hourly provider-health-probe cron needs a real file.
#
# Run this after any edit to scripts/, after a fresh clone, or to verify state:
#   scripts/sync_runtime.sh
set -euo pipefail

REPO_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_DIR="${HOME}/.hermes/scripts"
mkdir -p "${LIVE_DIR}"
drifted=""   # TR-256 drift gate accumulator (set -u: must exist before use)

# --- 1. symlinked tools (subprocess + manual consumers only) ---
# router_server.py + router_web.py joined TR-017/TR-018 (API+MCP server, web UI):
# long-lived processes exec'd by operators/systemd — symlink keeps them canonical.
for f in router_spawn.py router_circuit.py router_quota.py router_ledger.py router_seed.py router_maintain.py \
         router_quota_accounting.py router_quota_pacing.py router_quota_burn.py \
         router_modelsdev.py router_gaps.py router_pricing.py router_clinepass.py router_plan_sweep.py \
         router_learn.py router_server.py router_web.py router_status.py router_estimate.py \
         router_diff.py router_metrics.py router_validate.py router_probefix.py router_refresh_resume.py \
         router_health_probe.py router_proxy_stats.py router_ui_data.py router_rank_audit.py \
         router_release_backfill.py router_probe_ingest.py router_probe_run.py proxy_acceptance.py \
         policy_gate_audit.py; do
  target="${LIVE_DIR}/${f}"
  if [ -L "${target}" ] && [ "$(readlink "${target}")" = "${REPO_SCRIPTS}/${f}" ]; then
    echo "OK      ${f} -> symlink already correct"
  else
    rm -f "${target}"
    ln -s "${REPO_SCRIPTS}/${f}" "${target}"
    echo "LINKED  ${f} -> ${REPO_SCRIPTS}/${f}"
  fi
done

# --- 2. byte-identical copy (cron realpath guard: provider-health-probe AND
#        router-data-quality pipelines — cron resolves symlinks and BLOCKS any
#        script path resolves outside ~/.hermes/scripts/) ---
#
# router_validate.py is here (TR-REVIEW-001) because router_health.py now
# IMPORTS it in-process: a symlinked validator would make the health plane's
# gate verdict depend on a realpath outside ~/.hermes/scripts/, the exact shape
# the cron guard blocks, and a missing sibling module would silently turn the
# gate into an error block on every probe.
#
# sync_task_router_duckbrain.py is here (TR-204): the task-router-sync lane's
# canonical write path. The lane (agent or cron) invokes it from
# ~/.hermes/scripts/, so a symlink would put its realpath outside the scripts
# dir; the byte-identical copy keeps the repo as single source of truth.
# router_hop_taxonomy.py is here (TR-288 breakage 2026-10-08..10): the probe
# IMPORTS it (sys.path = ~/.hermes/scripts), so a repo-only module makes every
# hourly run die on ImportError and health.jsonl freezes — the copy list must
# carry every module on the probe's import path, not just entry points.
for f in provider_health_probe.py router_hop_taxonomy.py state_dir.py router-data-quality.sh fleet-cooldown-policy.py router_health.py router_validate.py sync_task_router_duckbrain.py; do
  want=644; [ "${f##*.}" = "sh" ] && want=755
  if [ "${f}" = "fleet-cooldown-policy.py" ]; then
    # ── SCHED-PERF-006 deploy-hash guard ──────────────────────────────────
    # Never blindly overwrite the canonical live copy. Three cases:
    #   1. live matches repo → OK
    #   2. live matches sidecar (canonical) → SKIP + FAIL (stale repo clobber)
    #   3. live diverges from sidecar → proceed (live was already non-canonical)
    SIDECAR="${LIVE_DIR}/.fleet-cooldown-policy.canonical.sha256"
    if [ -f "${LIVE_DIR}/${f}" ] && [ ! -L "${LIVE_DIR}/${f}" ] && cmp -s "${REPO_SCRIPTS}/${f}" "${LIVE_DIR}/${f}"; then
      echo "OK      ${f} -> byte-identical copy"
    else
      LIVE_HASH=""; CANONICAL_HASH=""
      if [ -f "${LIVE_DIR}/${f}" ] && [ ! -L "${LIVE_DIR}/${f}" ]; then
        LIVE_HASH=$(sha256sum "${LIVE_DIR}/${f}" | cut -c1-64)
      fi
      if [ -f "${SIDECAR}" ]; then
        # Sidecar stores the deployed-script hash as plain hex text (64 chars).
        # Read it directly — do NOT sha256sum the sidecar file itself.
        CANONICAL_HASH=$(tr -d '[:space:]' < "${SIDECAR}")
      fi
      if [ -n "$LIVE_HASH" ] && [ "$LIVE_HASH" = "$CANONICAL_HASH" ] && [ -n "$CANONICAL_HASH" ]; then
        echo "SKIP    ${f} -> live copy matches canonical sidecar, REPO DIFFERS (SCHED-PERF-006)"
        echo "         The sidecar protects the deployed script from stale-repo clobber."
        echo "         Re-canonicalize the repo script or update the sidecar explicitly."
        exit 1
      else
        rm -f "${LIVE_DIR}/${f}"   # drop symlink first: cp would write THROUGH it
        cp "${REPO_SCRIPTS}/${f}" "${LIVE_DIR}/${f}"
        chmod "$want" "${LIVE_DIR}/${f}"
        echo "SYNCED  ${f} -> copied from repo (non-canonical live copy replaced)"
      fi
    fi
  else
    if [ -f "${LIVE_DIR}/${f}" ] && [ ! -L "${LIVE_DIR}/${f}" ] \
       && [ "$(stat -c %a "${LIVE_DIR}/${f}")" = "$want" ] \
       && cmp -s "${REPO_SCRIPTS}/${f}" "${LIVE_DIR}/${f}"; then
      echo "OK      ${f} -> byte-identical copy"
    else
      rm -f "${LIVE_DIR}/${f}"   # drop symlink first: cp would write THROUGH it
      cp "${REPO_SCRIPTS}/${f}" "${LIVE_DIR}/${f}"
      chmod "$want" "${LIVE_DIR}/${f}"
      echo "SYNCED  ${f} -> copied from repo (byte-identical, mode $want)"
    fi
  fi
  # ── TR-256 drift gate: the copy step's own verification ────────────────
  # A copy that silently failed (partial write, wrong target, clobbered by a
  # concurrent editor between cp and now) must not read as success. This is
  # the gap dogfood run31 flagged (docs/dogfood/diagnostics.md): the
  # byte-copy list had no verify mode, so copy-list drift went unnoticed
  # until consumers hit it. Fail loudly AFTER the loop, listing every file.
  if ! cmp -s "${REPO_SCRIPTS}/${f}" "${LIVE_DIR}/${f}"; then
    drifted="${drifted} ${f}"
    echo "DRIFT   ${f} -> live copy differs from repo after sync"
  fi
done

# --- 3. drift gate (TR-256): copy-list files must be byte-identical ---
# SCHED-PERF-006's sidecar guard above protects fleet-cooldown-policy.py
# from STALE-REPO CLOBBER (it exits 1 before this gate when live matches
# the sidecar). This gate covers the complementary failure: the sync ran
# but live still diverges from repo. Zero drift here = wiring converged.
if [ -n "${drifted}" ]; then
  echo "FATAL: copy-list drift (live vs repo) — sync did NOT converge:"
  for f in ${drifted}; do
    echo "  diverged: ${LIVE_DIR}/${f}"
  done
  echo "  fix: reconcile scripts/<file> with its live copy, re-run sync_runtime.sh"
  exit 1
fi

echo "runtime wiring OK"
