#!/usr/bin/env bash
# sync_runtime.sh — repo/scripts → ~/.hermes/scripts canonical wiring (TR-004)
#
# Repo scripts/ is the single source of truth for the router runtime tools.
# The live installs the scheduler + cron call live at ~/.hermes/scripts/.
# Two topologies exist, by consumer:
#   1. SYMLINKS — router_spawn.py, router_circuit.py, router_ledger.py,
#      router_seed.py, router_maintain.py. Consumers: scheduler daemon
#      subprocess, foremen, manual CLI calls. No path guard — symlinks exec
#      the canonical file directly.
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

# --- 1. symlinked tools (subprocess + manual consumers only) ---
for f in router_spawn.py router_circuit.py router_ledger.py router_seed.py router_maintain.py \
         router_modelsdev.py router_gaps.py router_pricing.py router_clinepass.py router_plan_sweep.py; do
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
#        script whose real path falls outside ~/.hermes/scripts/) ---
for f in provider_health_probe.py router-data-quality.sh; do
  want=644; [ "${f##*.}" = "sh" ] && want=755
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
done

echo "runtime wiring OK"
