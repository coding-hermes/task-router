#!/usr/bin/env bash
# scripts/muster_battery.sh — TR-127 live muster-consumer acceptance battery.
#
# The 7 probes of docs/muster-spec.md §4 (frozen from docs/muster-scope.md):
# spec published, muster ingests it, MCP tools generate, reads execute live,
# mutations are gated SERVER-side (state untouched), the two MCP surfaces
# agree, and consumers always resolve the base URL from the live spec.
#
# Usage:
#   bash scripts/muster_battery.sh
#   ROUTER_BASE=http://127.0.0.1:9092 MUSTER_BIN=/home/kara/wojons/muster/bin \
#       bash scripts/muster_battery.sh
#
# Environment:
#   ROUTER_BASE  router base URL (default http://127.0.0.1:9092)
#   MUSTER_BIN   directory holding muster's openapi-cli / openapi-mcp
#                (default /home/kara/wojons/muster/bin — the binaries MOVED
#                from the muster-scope doc's /home/kara/muster/bin; TR-127
#                correction, 2026-10-14)
#
# Exit codes: 0 = every run probe passed (SKIP allowed); 1 = at least one
#             FAIL; 2 = every probe skipped (nothing verified).
#
# Safety: probe 5 fires a real mutation CALL. The battery runs it ONLY when
# /health reports mode "read-only" (otherwise SKIP, loudly) and asserts the
# server refuses it — circuit state must be byte-identical before/after.
# The battery never needs an edit key and never mutates router data.

set -uo pipefail

ROUTER_BASE="${ROUTER_BASE:-http://127.0.0.1:9092}"
MUSTER_BIN="${MUSTER_BIN:-/home/kara/wojons/muster/bin}"
SPEC_URL="$ROUTER_BASE/openapi.json"

PASS=0; FAIL=0; SKIP=0
WORK="$(mktemp -d /tmp/muster-battery.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

pass() { PASS=$((PASS+1)); echo "PASS  probe $1 — $2"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL  probe $1 — $2"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP  probe $1 — $2"; }

have_muster() {
    [ -x "$MUSTER_BIN/openapi-cli" ] && [ -x "$MUSTER_BIN/openapi-mcp" ]
}

require_muster() {  # $1 = probe number
    if ! have_muster; then
        skip "$1" "muster binaries missing at $MUSTER_BIN (openapi-cli / openapi-mcp)"
        return 1
    fi
    return 0
}

# Ground-truth operationId set, derived from the LIVE spec (never hardcoded:
# the count is whatever the running router publishes).
spec_op_ids() {
    jq -r '[.paths | to_entries[] | .value | to_entries[]
            | select((.key | ascii_downcase) as $m
                     | ["get", "post", "put", "patch", "delete"] | index($m))
            | .value.operationId]
           | sort | .[]' "$WORK/spec.json"
}

# One stdio MCP session against openapi-mcp. $1 = stdout file, $2 = stderr
# file, rest = one JSON-RPC request per argument. MCP_TIMEOUT bounds the
# session (default 30s): per the fail-open contract, a hang is a FAIL.
mcp_session() {
    local out="$1" err="$2"; shift 2
    printf '%s\n' "$@" \
        | timeout "${MCP_TIMEOUT:-30}" "$MUSTER_BIN/openapi-mcp" --spec "$SPEC_URL" \
        > "$out" 2> "$err"
}

REQ_INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"muster-battery","version":"1.0"}}}'
REQ_INITED='{"jsonrpc":"2.0","method":"notifications/initialized"}'

# ---------------------------------------------------------------------------
probe1() {
    require_muster 1 || return 0
    if ! "$MUSTER_BIN/openapi-cli" discover "$SPEC_URL" \
            > "$WORK/discover.txt" 2> "$WORK/discover.err"; then
        fail 1 "openapi-cli discover exited non-zero: $(head -c 200 "$WORK/discover.err")"
        return
    fi
    local missing=0 id
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        if ! grep -qw "$id" "$WORK/discover.txt"; then
            echo "        probe 1: missing from discover output: $id"
            missing=$((missing+1))
        fi
    done < "$WORK/op-ids.txt"
    local n_want n_seen
    n_want=$(grep -c . "$WORK/op-ids.txt" | tr -d ' ')
    n_seen=$(grep -cE '^[[:space:]]*(GET|POST|PUT|PATCH|DELETE)[[:space:]]' "$WORK/discover.txt" | tr -d ' ')
    if [ "$missing" -eq 0 ] && [ "$n_seen" -eq "$n_want" ]; then
        pass 1 "muster ingests the live spec: $n_seen operations listed, all operationIds present"
    else
        fail 1 "discover mismatch: $missing operationIds missing, $n_seen operations listed (spec says $n_want)"
    fi
}

probe2() {
    require_muster 2 || return 0
    if ! mcp_session "$WORK/tools.jsonl" "$WORK/tools.stderr" \
            "$REQ_INIT" "$REQ_INITED" \
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; then
        fail 2 "openapi-mcp session failed or timed out (a hang is a FAIL)"
        return
    fi
    local sname
    sname=$(jq -r 'select(.id==1) | .result.serverInfo.name // empty' "$WORK/tools.jsonl")
    if [ "$sname" != "openapi-mcp" ]; then
        fail 2 "initialize serverInfo.name='$sname' (want openapi-mcp)"
        return
    fi
    jq -r 'select(.id==2) | .result.tools[].name' "$WORK/tools.jsonl" | sort \
        > "$WORK/tool-names.txt"
    local n_got n_want empty_schema
    n_got=$(grep -c . "$WORK/tool-names.txt" | tr -d ' ')
    n_want=$(grep -c . "$WORK/op-ids.txt" | tr -d ' ')
    empty_schema=$(jq -s '[.[] | select(.id==2) | .result.tools[]
                          | select((.inputSchema // {}) | length == 0)] | length' \
                      "$WORK/tools.jsonl")
    if diff -u "$WORK/op-ids.txt" "$WORK/tool-names.txt" \
            > "$WORK/names.diff" 2>&1; then
        if [ "$empty_schema" -eq 0 ]; then
            pass 2 "consumer block generates the full tool set: $n_got tools, name set == operationId set (set equality), every inputSchema non-empty"
        else
            fail 2 "$empty_schema tools carry an empty inputSchema"
        fi
    else
        fail 2 "tool-name set != operationId set ($n_got tools vs $n_want operationIds); diff: $(head -c 200 "$WORK/names.diff")"
    fi
}

probe3() {
    require_muster 3 || return 0
    if ! mcp_session "$WORK/health.jsonl" "$WORK/health.stderr" \
            "$REQ_INIT" "$REQ_INITED" \
            '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"getHealth","arguments":{}}}'; then
        fail 3 "openapi-mcp session failed or timed out"
        return
    fi
    local is_err
    is_err=$(jq -r 'select(.id==3) | (.result.isError // false)' "$WORK/health.jsonl")
    if [ "$is_err" != "false" ]; then
        fail 3 "getHealth returned isError=$is_err"
        return
    fi
    jq -r 'select(.id==3) | .result.content[0].text // empty' "$WORK/health.jsonl" \
        > "$WORK/health-body.json"
    if ! jq -e 'type == "object"' "$WORK/health-body.json" > /dev/null 2>&1; then
        fail 3 "content[0].text does not parse as a JSON object"
        return
    fi
    if jq -e '.status == "ok" and (.registry.models | type == "number" and . > 0)
              and .registry.age_days == 0' "$WORK/health-body.json" > /dev/null; then
        pass 3 "getHealth through MCP: status=ok, registry.models=$(jq -r '.registry.models' "$WORK/health-body.json"), age_days=0 (live, fresh)"
    else
        fail 3 "health payload not live-and-fresh: $(head -c 200 "$WORK/health-body.json")"
    fi
}

probe4() {
    require_muster 4 || return 0
    local project
    project=$(curl -fsS -m 10 "$ROUTER_BASE/profiles" 2> /dev/null \
        | jq -r '.profiles[0].id // empty' 2> /dev/null)
    if [ -z "$project" ]; then
        skip 4 "no profile id obtainable from GET $ROUTER_BASE/profiles"
        return
    fi
    local req
    req=$(jq -nc --arg p "$project" \
        '{jsonrpc:"2.0",id:4,method:"tools/call",
          params:{name:"resolve",arguments:{project:$p}}}')
    if ! MCP_TIMEOUT=60 mcp_session "$WORK/resolve.jsonl" "$WORK/resolve.stderr" \
            "$REQ_INIT" "$REQ_INITED" "$req"; then
        fail 4 "resolve session timed out — a hang traps the agent loop (PROTOCOLS.md §4.5 rule 1)"
        return
    fi
    jq -r 'select(.id==4) | .result.content[0].text // empty' "$WORK/resolve.jsonl" \
        > "$WORK/resolve-body.json"
    if ! jq -e 'type == "object"' "$WORK/resolve-body.json" > /dev/null 2>&1; then
        fail 4 "resolve returned an unparseable tool result: $(head -c 200 "$WORK/resolve-body.json")"
        return
    fi
    # Fail-open contract point: a well-formed API error IS an acceptable
    # outcome (the router may have no data for this project). Only a hang, a
    # crash, or an unparseable result is a FAIL.
    local n_hops err
    n_hops=$(jq -r '(.chain // []) | length' "$WORK/resolve-body.json")
    err=$(jq -r '.error // empty' "$WORK/resolve-body.json")
    if [ -n "$err" ]; then
        pass 4 "resolve through MCP: well-formed error (fail-open OK): $err"
    else
        pass 4 "resolve through MCP: well-formed chain for '$project' ($n_hops hops)"
    fi
}

probe5() {
    if [ "$ROUTER_MODE" != "read-only" ]; then
        skip 5 "router mode is '$ROUTER_MODE' — probe 5 fires a real mutation call and may only run against read-only"
        return
    fi
    require_muster 5 || return 0
    local before after
    before=$(curl -fsS -m 10 "$ROUTER_BASE/circuit/status" 2> /dev/null \
        | jq -Sc 'del(.ts)' 2> /dev/null | sha256sum | cut -d" " -f1)
    if [ -z "$before" ]; then
        skip 5 "cannot read GET $ROUTER_BASE/circuit/status for the before-snapshot"
        return
    fi
    local req
    req=$(jq -nc '{jsonrpc:"2.0",id:5,method:"tools/call",
          params:{name:"recordCircuit",arguments:{
            provider:"probe-provider", model:"probe-model",
            outcome:"failure", reason:"muster acceptance probe"}}}')
    if ! mcp_session "$WORK/mut.jsonl" "$WORK/mut.stderr" \
            "$REQ_INIT" "$REQ_INITED" "$req"; then
        fail 5 "mutation session failed or timed out"
        return
    fi
    local is_err txt ok=1
    is_err=$(jq -r 'select(.id==5) | (.result.isError // false)' "$WORK/mut.jsonl")
    txt=$(jq -r 'select(.id==5) | .result.content[0].text // ""' "$WORK/mut.jsonl")
    [ "$is_err" = "true" ] || { echo "        probe 5: isError=$is_err (want true)"; ok=0; }
    case "$txt" in *"HTTP 403"*) ;; *) echo "        probe 5: text lacks 'HTTP 403'"; ok=0;; esac
    case "$txt" in *"read-only mode"*) ;; *) echo "        probe 5: text lacks 'read-only mode'"; ok=0;; esac
    after=$(curl -fsS -m 10 "$ROUTER_BASE/circuit/status" 2> /dev/null \
        | jq -Sc 'del(.ts)' 2> /dev/null | sha256sum | cut -d" " -f1)
    [ -n "$after" ] || { echo "        probe 5: cannot read the after-snapshot"; ok=0; }
    [ "$before" = "$after" ] || { echo "        probe 5: circuit state CHANGED across the refused call"; ok=0; }
    if [ "$ok" -eq 1 ]; then
        pass 5 "mutation boundary holds: isError=true, 'HTTP 403' + 'read-only mode' in text, circuit state byte-identical before/after"
    else
        fail 5 "the server-side gate did not hold (see detail lines above)"
    fi
}

probe6() {
    # POST /mcp bridge parity (the drift alarm while both bridges exist;
    # inverts to a 404 assertion after open decision #1 rules retirement).
    local req
    req=$(jq -nc '{jsonrpc:"2.0",id:6,method:"tools/list"}')
    if ! curl -fsS -m 10 -X POST "$ROUTER_BASE/mcp" \
            -H 'Content-Type: application/json' -d "$REQ_INIT" \
            -o "$WORK/bridge-init.json" 2> /dev/null; then
        skip 6 "POST $ROUTER_BASE/mcp unreachable (bridge retired? if TR-128 landed, this probe must be flipped to assert 404)"
        return
    fi
    if ! curl -fsS -m 10 -X POST "$ROUTER_BASE/mcp" \
            -H 'Content-Type: application/json' -d "$req" \
            -o "$WORK/bridge-tools.json" 2> /dev/null; then
        fail 6 "POST /mcp tools/list failed"
        return
    fi
    jq -r '.result.tools[].name' "$WORK/bridge-tools.json" 2> /dev/null | sort \
        > "$WORK/bridge-names.txt"
    if [ ! -s "$WORK/bridge-names.txt" ]; then
        fail 6 "POST /mcp answered without a tools list"
        return
    fi
    local n_bridge n_ref ref_label
    n_bridge=$(grep -c . "$WORK/bridge-names.txt" | tr -d ' ')
    if have_muster && [ -s "$WORK/tool-names.txt" ]; then
        ref="$WORK/tool-names.txt"; ref_label="muster tool set"
    else
        ref="$WORK/op-ids.txt"; ref_label="spec operationIds (muster absent)"
    fi
    n_ref=$(grep -c . "$ref" | tr -d ' ')
    if diff -u "$ref" "$WORK/bridge-names.txt" > "$WORK/bridge.diff" 2>&1; then
        pass 6 "bridge parity: POST /mcp serves the same $n_bridge tool names as the $ref_label"
    else
        fail 6 "bridge DRIFT: POST /mcp serves $n_bridge tools vs $n_ref in the $ref_label; diff: $(head -c 200 "$WORK/bridge.diff")"
    fi
}

probe7() {
    require_muster 7 || return 0
    if ! mcp_session "$WORK/fresh.jsonl" "$WORK/fresh.stderr" \
            "$REQ_INIT" "$REQ_INITED" \
            '{"jsonrpc":"2.0","id":7,"method":"tools/list"}'; then
        fail 7 "openapi-mcp session failed or timed out"
        return
    fi
    if grep -Fq "Using base URL: $ROUTER_BASE (override=false)" "$WORK/fresh.stderr" \
            && grep -Fq "Loaded spec:" "$WORK/fresh.stderr"; then
        pass 7 "canonical spec freshness: muster resolved the base URL from the live URL spec (override=false)"
    else
        fail 7 "stderr lacks the URL-spec resolution line: $(head -c 200 "$WORK/fresh.stderr")"
    fi
}

# ---------------------------------------------------------------------------
echo "== muster acceptance battery — $SPEC_URL =="
echo "    muster binaries: $MUSTER_BIN"

if ! curl -fsS -m 5 "$ROUTER_BASE/health" > "$WORK/health.json" 2> /dev/null; then
    for n in 1 2 3 4 5 6 7; do
        skip "$n" "router down at $ROUTER_BASE (GET /health failed)"
    done
    echo "== battery summary: 0 passed, 0 failed, 7 skipped — NOTHING VERIFIED =="
    exit 2
fi
ROUTER_MODE=$(jq -r '.mode // "unknown"' "$WORK/health.json")
echo "    router mode: $ROUTER_MODE (probe 5 runs only against read-only)"

if ! curl -fsS -m 10 "$SPEC_URL" -o "$WORK/spec.json" 2> /dev/null; then
    for n in 1 2 3 4 5 6 7; do
        skip "$n" "cannot fetch $SPEC_URL"
    done
    echo "== battery summary: 0 passed, 0 failed, 7 skipped — NOTHING VERIFIED =="
    exit 2
fi
spec_op_ids > "$WORK/op-ids.txt"
echo "    live spec: $(grep -c . "$WORK/op-ids.txt" | tr -d ' ') operations"

probe1
probe2
probe3
probe4
probe5
probe6
probe7

echo "== battery summary: $PASS passed, $FAIL failed, $SKIP skipped =="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
if [ "$PASS" -eq 0 ]; then
    echo "NOTHING VERIFIED: every probe skipped"
    exit 2
fi
exit 0
