#!/bin/bash
# Hourly provider-health tick: probe -> dashboard refresh -> link-only delivery.
#
# Why a wrapper: the probe's full report is ~200 lines and Bane wants the chat
# quiet (2026-10-03). Full report is preserved in the log; the chat gets a few
# lines + the dashboard URL. Failure modes still speak: probe errors are echoed.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin"
export ROUTER_STATE_DIR="${ROUTER_STATE_DIR:-$HOME/.hermes/model-router}"
cd "$HOME/.hermes" || exit 1

PY=/usr/bin/python3
LOGDIR="$HOME/.hermes/logs"
LOG="$LOGDIR/provider-health-report.log"
mkdir -p "$LOGDIR"

# 1. probe — writes health-state.json + appends health.jsonl (data of record)
PROBE_OUT="$($PY "$HOME/.hermes/scripts/provider_health_probe.py" 2>&1)"
rc=$?
{ printf '=== %s rc=%s ===\n' "$(date -u +%FT%TZ)" "$rc"; printf '%s\n' "$PROBE_OUT"; } >> "$LOG"
# bound the log (keep the last ~600 reports of history)
if [ "$(wc -l < "$LOG")" -gt 12000 ]; then
  tail -n 6000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 2. dashboard refresh (independent of probe success — it renders whatever is on disk)
$PY "$HOME/.hermes/scripts/provider_health_dashboard.py" --quiet >/dev/null 2>&1
drc=$?

# 3. what the chat sees
$PY "$HOME/.hermes/scripts/provider_health_summary.py"
if [ "$rc" -ne 0 ]; then
  printf '!! probe exited rc=%s — last line of its output:\n' "$rc"
  printf '%s\n' "$PROBE_OUT" | tail -n 3
  printf 'full output: %s\n' "$LOG"
fi
if [ "$drc" -ne 0 ]; then
  printf '!! dashboard refresh failed rc=%s (stale page served)\n' "$drc"
fi
exit 0
