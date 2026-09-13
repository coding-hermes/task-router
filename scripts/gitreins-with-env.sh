#!/usr/bin/env bash
# TR-046b: gitreins' LLM client falls back to https://api.openai.com/v1 when
# GITREINS_LLM_BASE_URL is unset (engine/llm.py), so any clean shell — cron-spawned
# foreman tick, hermes chat worker — that runs `gitreins task complete` sends the
# deepseek key to api.openai.com and eats a guaranteed 401 (tick 40: verdict
# INCOMPLETE, evaluator 401 on api.openai.com, 3 attempts).
#
# The credentials live in ONE place: ~/.hermes/.env. This wrapper loads the
# GITREINS_* variables from that file (without printing values), then execs
# gitreins. Values already present in the environment win — the file only
# fills gaps.
#
# Usage: scripts/gitreins-with-env.sh task complete TR-046b
set -euo pipefail

ENV_FILE="${GITREINS_ENV_FILE:-$HOME/.hermes/.env}"

if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" == export\ * ]]; then line="${line#export }"; fi
    key="${line%%=*}"
    [[ "$key" == "$line" ]] && continue          # no '=' on the line -> not an assignment
    val="${line#*=}"
    case "$key" in
      GITREINS_LLM_API_KEY|GITREINS_LLM_BASE_URL|GITREINS_LLM_MODEL|GITREINS_OPENROUTER_KEY)
        if [[ -z "${!key:-}" ]]; then export "$key=$val"; fi
        ;;
    esac
  done < "$ENV_FILE"
fi

exec gitreins "$@"
