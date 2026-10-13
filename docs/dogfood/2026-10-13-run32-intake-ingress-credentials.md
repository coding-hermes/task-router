# Dogfood run 32 — 2026-10-13 — automatic intake, bus ingress, credential acceptance

Angle: the three surfaces no prior run touched — TR-298 automatic board task
intake, TR-236 bus ingress, and the TR-223 credential-acceptance fix in
`router_rank_audit.py` (all landed after the 2026-10-01 run).

Verdict: SHIPPABLE. Every promised behavior held under real use; the findings
are friction, not breakage. Rows filed: TR-319, TR-320, TR-321.

## Setup (scratch, zero live-state writes)

    python3 -m venv /tmp/tr-df-20261013/venv
    /tmp/tr-df-20261013/venv/bin/pip install -e /home/kara/task-router duckdb
    export TASK_ROUTER_HOME=/tmp/tr-df-20261013/home   # registry + CLI state
    export ROUTER_STATE_DIR=/tmp/tr-df-20261013/gate   # quota/health/circuit plane (see TR-319!)
    cp ~/.hermes/model-router/{quota-state,circuit-state}.json gate/

`router seed` 16.6s → registry.json 4.4MB, then `router spawn P1_CODING`
resolves a real chain (head xkiro/z-ai/glm-5.3-flash).

**Two-plane lesson (TR-319):** TASK_ROUTER_HOME alone is NOT a full sandbox.
scripts/router_spawn.py takes its gate plane from ROUTER_STATE_DIR, whose
default is the LIVE ~/.hermes/model-router. Our first intake pass ran with a
half-isolated home and produced 48/48 "resolved" rows with chain_head null —
exit 0, errors 0, everything LOOKED fine. A scratch home needs BOTH env vars.

## TR-298 automatic intake (works; idempotence verified)

    venv/bin/python scripts/board_task_intake.py --board <copy> --state-dir state --once

Run 1 (broken gate plane): new=48 resolved=48 errors=0 — all head None.
Run 2 (complete plane): new=48 resolved=48 errors=0 — real heads
(xkiro/z-ai/glm-5.3-flash ×29, muse-spark ×19). Replay: new=0 (idempotent).
One appended pending row → exactly one resolve, claim+complete in
intake-seen.jsonl (2 rows per id, LAST wins — as documented). Backlog drain
48 tasks ≈ 32s (≈0.67s/task, JEV complexity scoring included).

## TR-236 bus ingress (forward + serve verified end-to-end)

Happy path: forward → hermes-gateway-local (:8642 /v1/responses SSE,
auth via env-file reference) → "INGRESS OK" in 3.56s; ledger row carries
endpoint_served, model_chosen == model_served, tokens 49501/8, cost_usd null
with null_reasons "not reported by endpoint". Push door: /ingress/v1/messages
→ 401 no token, 401 wrong token, valid push → gateway → bus_delivery
ok:orchestrator in 3.9s. Refusal ladder — all named, all ledgered, none
silent: unknown-endpoint, endpoint-incomplete (missing timeout_s),
auth-unresolved (env:NAME empty), no-prompt, duplicate (idempotency_key,
second attempt never forwarded), unauthorized ×2. Circuit: under ONE serve
process, 3 transport errors then endpoint-circuit-open on the 4th (30s),
exactly per contract. Under repeated one-shot forward calls the breaker
never trips (per-process state — TR-321).

The openai-compatible door against a real multi-model server (LM Studio,
38 models): model:'router' → 400 invalid model identifier; model:'none' →
400 "specify a model". Both failures honest, but no third option exists —
TR-320.

## TR-223 credential acceptance (verified live in scratch)

    venv/bin/python scripts/router_rank_audit.py            # text
    venv/bin/python scripts/router_rank_audit.py --json | jq .credential_blocked_unranked
    venv/bin/python scripts/router_rank_audit.py --next-wave

Text report: "CREDENTIAL-BLOCKED (cannot be measured until the key is
rotated): {'openrouter': 274} — reason: credential-expired". JSON carries
credential_blocked_unranked {"openrouter": 274}. --next-wave lists 54 lanes
across 9 providers with ZERO openrouter entries. The fix does exactly what
its commit says.

## Performance (Step 2b)

    hyperfine --warmup 3 --runs 20 'router spawn P1_CODING --format json'
    → 161.4 ms ± 15.2 ms (range 138.8–201.7)
    cold (P0_FORE, one-shot): 0.15s

Comfortably fast; a user would never notice. No PERF row filed — a win
nobody can feel is not a finding.

## Ephemeral bunker install leg

Host: bunker-mvp (las-03 and las-02/04 offline at tick time; mvp active).
`bunker spawn --server bunker-mvp --ttl 1h` → agent 299229b7, 21.6s.
Public clone (github.com/coding-hermes/task-router, HEAD 93ef98f = local
HEAD) — the documented quickstart's first command works from inside the
agent. Then, verbatim, inside the agent: python3 -m venv && pip install -e .
&& router status (rc=0) && pip install duckdb && router seed (rc=0) &&
router spawn P1_CODING (rc=0, real chain head) && router validate (14/14).
Clone→install→seed→spawn→validate = 39.5s on the fresh box. Agent destroyed;
residue recheck after 90s: 0.
