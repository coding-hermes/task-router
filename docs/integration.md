# Scheduler integration spec — TASK-ROUTER-001/002 (2026-08-26)

Filed on the coding-hermes-scheduler board by router ops. The scheduler foreman
implements; router ops owns the tools + data.

## TASK-ROUTER-001 — spawn-time model/provider resolution
- Before building the gateway POST (`/v1/responses`), call:
  `~/.hermes/venvs/board/bin/python3 ~/.hermes/scripts/router_spawn.py <project> --format json`
- `<project>` is a registry project id; a bare PROFILE id/tag in that slot also
  resolves (TR-059) — same semantics as `--profile`, so `spawn P1_CODING` and
  `spawn --profile P1_CODING` return the same chain (the payload's `resolved_as`
  says which path was taken, and `hint` names the canonical form when the slot
  carried a profile name).
- Use the returned `head.provider` / `head.model` in the request body.
- Router errors → fall back to current per-project model/provider behavior (fail-open, log warning).
- PAYG (deepseek) is a LEGITIMATE fallback hop — subs first by price order; never force PAYG as
  default primary when a healthy sub exists.
- Per-project foreman profiles: routing registry `projects.profile` (P0_FORE default) → requirements.

## TASK-ROUTER-002 — circuit breaker + retry/backoff + health exclusion
- Spawn/tick failure → `router_circuit.py record-failure <provider> <model> [reason]`
- Success → `router_circuit.py record-success <provider> <model>`
- Before spawn: pairs with OPEN circuit (open_until future) or provider DOWN/SLOW in
  health-state are excluded — router_spawn.py already applies both (--health default on).
- Do NOT retry the same pair while its breaker is open — advance to the next chain hop.
- Existing fallback_model / fallback_provider / consecutive_failures columns stay the
  last-resort fallback when the router is unavailable.
- Breaker cooldowns are FLAT per failure class (router_circuit.py
  CLASS_COOLDOWN_S): overload=120s, quota_window=300s, api_down=1800s,
  out_of_credit=14400s. Consecutive failures re-open with the same class
  cooldown — no exponential doubling.
- Max 1 spawn attempt per hop per tick.

## Diversity + concurrency (TR-007 design, 2026-08-27)

Two diversity knobs prune the price-ordered eligible chain AFTER gates; both are
global defaults with per-profile overrides, and no caps configured = output
identical to pre-TR-007:

- Where the knobs live:
  - Global: `~/.hermes/model-router/quota-state.json` → optional `"diversity"`
    key: `{"max_consecutive_per_provider": N|null, "max_total_per_provider":
    N|null, "model_concurrency_limit": N|null}` (`null`/absent = unbounded) and
    optional per-pair limits under `"models"`:
    `{"<provider>/<model>": {"concurrency_limit": N}}` (explicit pair limit
    beats the global model_concurrency_limit).
  - Per-profile overrides: `task_profiles.max_consecutive_per_provider /
    max_total_per_provider` columns (NULL = fall back to global). Profile beats
    global.
- Semantics: applied as PRUNING — router_spawn.py walks the price-ordered
  survivor chain, drops violators, reports each drop in `exclusions` +
  `gate_reasons` (`'consecutive cap N'`, `'chain cap N'`). Price order among
  survivors is preserved. NEVER a provider-wide pre-filter.
- Busy-skip semantics (per-MODEL, not per-provider): a model at its concurrency
  limit is skipped individually like a circuit exclusion (`'model busy (k
  in-flight >= limit N)'`); the provider's other models stay eligible. A busy
  model NEVER removes a cheap provider whose sibling is free.
- Ledger contract for the scheduler (TASK-ROUTER-002 call side):
  1. Before spawn: `router_ledger.py start --provider P --model M [--project X]
     [--profile R] [--hop N] [--reason R]` → prints a trace_id on stdout;
     capture it.
  2. After the spawn settles: `router_ledger.py end --trace-id T --outcome
     success|failure|error [--latency-ms N] [--error-class E] [--tokens-in N]
     [--tokens-out N]`. Invalid outcome exits 2; unknown trace_id warns but
     still appends (fail-open).
  3. In-flight counts derive from ledger.jsonl: a trace whose last row is
     outcome='started' is in flight; 'started' rows older than 30 minutes are
     stale (crash without `end`) and do not count. `router_ledger.py status
     [--provider P] [--json]` shows per-(provider, model) in-flight + last
     outcome.
- Every routed call should get a ledger row (schema v2 subset — fields present
  only when known; never fabricated).

## Ledger status: NOT WIRED (TR-026 decision, 2026-08-28)

**The spawn ledger is not wired. Concurrency accounting is INACTIVE.** The
scheduler does not call `router_ledger.py start/end` around spawns, so
`ledger.jsonl` has zero trace rows, `ledger_in_flight()` returns `{}`, and the
TR-007 `'model busy'` gate can never fire. The TR-007 diversity/concurrency
knobs (`max_consecutive_per_provider`, `max_total_per_provider`, per-model
concurrency limits) are a **no-op for concurrency** until wiring lands — they
still apply for diversity pruning, but nothing is ever skipped as `'model
busy'`.

**Decision (path B — visible disable):** wiring the ledger into the spawn path
requires calling start/end from the scheduler's spawn flow (spawn.go has the
tick-ID plumbing; integration is tracked on the coding-hermes-scheduler board
as TASK-ROUTER-002 call side). task-router must not touch scheduler Go code
(AGENTS.md), so this repo implements the loud-disable alternative instead:

- `router_ledger.py status` reports `"wired": false` (+ WARNING line) when
  zero trace rows exist, and `"wired": true` once a trace lands.
- `router_spawn.py` resolve output emits a `spawn ledger NOT WIRED` warning
  and `gates_loaded.ledger: false` whenever the ledger file has no rows, so
  every resolve consumer sees the concurrency gate is inactive.
- `gates_loaded.ledger_rows` still reports live in-flight counts — 0 until
  wired (TR-025 added the field; TR-026 adds the `ledger` wired flag).

**To wire (scheduler side, tracked there):** call `router_ledger.py start`
before each routed spawn, capture the trace_id, call `end` when the spawn
settles; `router_ledger.py status` then flips to `wired: true` automatically
and the `'model busy'` gate starts enforcing per-model concurrency limits.

## TR-066 — Side channel (declared complexity → chain → caller executes)

The cheap integration path: the caller ALREADY knows the complexity (the task
board holds the profile), so no classifier and no proxy are involved. The
caller asks the router for a chain and executes it itself.

**1. Ask for the chain** (any of these; response facts are the same):

```bash
# named profile from the board
python3 scripts/router_spawn.py <project> --format json
python3 scripts/router_spawn.py --profile P1_CODING --format json
# ad-hoc category levels (the DEPLOYED complexity: cat=level, integers)
python3 scripts/router_spawn.py --profile-req 'code_gen=2 test=1' --format json
# stats-based ordering (TR-065) + backend isolation + window
... --sort predicted_cost_per_task --window-h 24 --backend hermes
... --sort 'ratio:0.7*cost+0.3*turns'
```

**The board row can declare its own profile (TR-124).** A task row carrying
`"profile": "P1_CODING"` (exact id or tag) sets its own capability bar instead
of the project's default:

```bash
python3 scripts/router_spawn.py <project> --profile-from-board <task-id> \
    --board <workdir>/.coding-hermes/board/tasks.jsonl --format json
```

The row's declaration outranks the project row's default profile; rows without
the field resolve exactly as before. Fail-open by contract: missing field, missing
row, or an unknown profile id degrade VISIBLY to the normal profile path (the
payload's `board_profile` block carries matched/declared/problems) — the router
never blocks the scheduler over a board lookup. A matched declaration also skips
complexity scoring entirely, mirroring the proxy's declared-complexity
precedence (`x-router-profile`).

Per hop the response carries: `provider`, `model`, `usd_1m` (PUBLIC list price —
reporting), the chain position, `context_limit`, and `outcomes` (the stats
provenance: `matched`, `complexity_sig`, `n_samples`, `stats_fallback`,
predicted_cost_per_task, avg_wall_time/turns/tokens). `stats_fallback` NAMES
the weaker bucket when ordering rested on one (`unconditioned`, `merged-backends`,
`weighted-fallback`, `no-samples`, or `null` = exact signature match).

**2. Execute the chain** — reference implementation, one command:

```bash
python3 scripts/router_chain_run.py <project|--profile P|--profile-req '...'> \
    --sort predicted_cost_per_task --max-hops 3 \
    --cmd 'my-agent --provider {provider} --model {model} --prompt-file task.md'
```

- The template gets `ROUTER_PROVIDER`, `ROUTER_MODEL`, `ROUTER_HOP`,
  `ROUTER_KEY_ENV` in the environment — the CALLER owns auth; the router never
  handles keys.
- Exit 0 = success → stop; non-zero = transport/HTTP failure → record a breaker
  failure and advance to the next hop (bounded by `--max-hops`).
- **Content dissatisfaction is NOT a retry trigger.** A template that ran fine
  but produced a bad result must exit 0 and report `success=false` itself —
  the executor only walks the chain on transport failures.
- `--dry-run` prints the plan (hops, prices, stats provenance) with zero
  executions and zero writes.

**3. Write back the outcome** (closes the loop; required for the averages to
learn). Either the CLI or the API — same validation:

```bash
python3 scripts/router_outcomes.py ...
curl -X POST localhost:9092/api/v1/outcomes -H 'Content-Type: application/json' -d '{
  "source_system":"hermes","session_id":"s-123","profile_id":"P1_CODING",
  "required_categories":{"code_gen":2,"test":1},
  "provider":"deepseek","model":"deepseek-v4-flash",
  "turns":7,"tokens_in":52000,"tokens_out":3100,"cost_usd":0.0112,
  "wall_time_s":184.5,"success":true}'
```

Rows are keyed `(source_system, session_id, model)` — a retried POST never
double-counts. `router_chain_run.py` writes one row per attempt automatically
(source_system `chain-run`) and forwards breaker evidence to the circuit store.

**4. Refresh the averages** (hourly, already wired): cron job
`task-router averages refresh` runs `~/.hermes/scripts/router-averages-refresh.sh`
(silent on success, alerts on failure) — newly reported outcomes are in the
stats within the hour.

## JEV — the cheap second scorer (TR-101, Bane 2026-09-20)

Same proxy, same matrix machinery, different (much cheaper) brain for the
"how hard is this input?" question. One JEV decisions call answers a `score`
question on a documented 0..2 scale (1 = the middle) and the score selects a
BAND whose LEVELS are the complexity matrix — so chain selection is unchanged.

```bash
export OR_JEV=<key from the "Jev" OpenRouter workspace>   # ~/.hermes/.env
curl -s localhost:9391/v1/chat/completions -H "Authorization: Bearer $GATEWAY_KEY" \
  -H 'x-router-scorer: jev' -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"why does this test deadlock under -race?"}]}'
```

Selection: per-request header `x-router-scorer: jev` beats the deployment
default `ROUTER_SCORER` (`classifier` when unset). A declared profile
(`x-router-profile`) still skips scoring entirely — it keeps priority.

Scalar limits, stated rather than hidden: JEV answers ONE number, so it cannot
know WHICH categories a task stresses; a band therefore carries a fixed
(coding-oriented) matrix and the result reports `band` / `band_title` /
`band_note` so the coarse read is visible in every outcome row. Edit
`data/classifier/jev-bands.jsonl` to retune — the scale is DATA, not code.

Measured on this box (2026-09-20, `OR_JEV` workspace key):

| scorer | input | answer | cost/call | matrix produced |
|---|---|---|---|---|
| classifier (TR-067) | prompt file + chat model | per-category JSON | model tokens | `{debug:3, reasoning:3, code_gen:1, test:2}` |
| **JEV** | one decisions call | `score 1.88`, confidence 0.82 | **$0.0000144** | `{code_gen:0, debug:1, refactor:0, test:1, reasoning:0}` |

Both paths were verified against a real request on 2026-09-20; they produced
DIFFERENT matrices and therefore different chains (classifier → `zai-glm/glm-5.3-flash`,
JEV → `stepfun/step-3.7-flash`) — the scorer really does steer routing.

**Deploy recipe that works (verified, incl. the trap):** the classifier/key
lookup reads the process environment, so a server started with the key only
present in `~/.hermes/.env` degrades with `HTTP 401` — export it first:

```bash
set -a; source ~/.hermes/.env; set +a          # transient, never persisted
export ROUTER_PROXY_AUTH=passthrough            # mirror mode: the caller's own upstream key is the gate
export ROUTER_PROXY_UPSTREAM=http://127.0.0.1:8642
export ROUTER_CLASSIFIER_BASE_URL=http://127.0.0.1:8642/v1
export ROUTER_CLASSIFIER_MODEL=deepseek-v4.1-flash
export ROUTER_CLASSIFIER_KEY_ENV=API_SERVER_KEY
python3 scripts/router_server.py --mode read-only --host 127.0.0.1 --port 9391
```

## TR-067 — Router proxy (classified complexity → internal chain → upstream call)

The zero-effort path: the client points its `base_url` at the router and
nothing else changes. The router classifies the request, builds the chain
internally, calls the upstream gateway, and walks the chain on transport
failures.

**1. Configure (all DATA, all env):**

```bash
export ROUTER_PROXY_UPSTREAM=http://127.0.0.1:8642        # the real gateway
export ROUTER_CLASSIFIER_BASE_URL=https://api.z.ai/api/coding/paas/v4
export ROUTER_CLASSIFIER_MODEL=glm-5.3-flash              # fast sub lane
export ROUTER_CLASSIFIER_KEY_ENV=ZAI_API_KEY              # NAME, never a value
export ROUTER_PROXY_MAX_HOPS=3
```

The classifier prompt is a VERSIONED FILE (`data/classifier/prompt-v1.md`) —
edit the file, not the code; every result records the prompt version + model it
used. Without `ROUTER_CLASSIFIER_BASE_URL` the proxy still works: it degrades
VISIBLY to the default profile (`_router.degrade_reason` says why).

> **`ROUTER_CLASSIFIER_KEY_ENV` names an env var — it is deployment-specific.**
> The value above (`ZAI_API_KEY`) is this fleet's name for the Z.AI credential;
> on another deployment the correct name is whatever that `.env` actually
> defines. If the name is wrong the classifier call cannot authenticate and the
> proxy falls back to the default profile — a *visible* degrade
> (`_router.degrade_reason` names the failure), but the cost ranking is silently
> off. Check the key name as part of verification.

**2. Call it like the gateway:**

```bash
curl -s localhost:9092/v1/chat/completions -H 'Authorization: Bearer <your-gateway-key>' \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"why is this Go test deadlocking under -race?"}]}'
```

Headers that steer the router (all optional):

| header | effect |
|---|---|
| `x-router-profile: P1_CODING` | DECLARED complexity — skips the classifier entirely (contract face) |
| `x-router-sort: predicted_cost_per_task` | ordering rule (any TR-065 metric or `ratio:` mix) |
| `x-router-window-h: 24` | decay window for stats-based ordering |
| `x-router-max-hops: 2` | bound on the fallback ladder |

The caller's `Authorization` / `x-api-key` are forwarded upstream unchanged —
the router never stores or invents keys.

**Auth.** Every POST to the server is key-gated by default (read-only mode
answers `403`, edit mode needs `X-API-Key`). For a gateway-shaped client
whose own credential is the real gate, start the server with
`ROUTER_PROXY_AUTH=passthrough` — that skips ONLY the router key on the two
mirror paths (never on mutations), and the server stays bound to localhost.

**3. Read the response.** The upstream response shape is returned as-is, plus
an additive `_router` object:

```json
"_router": {
  "complexity_source": "classifier | declared | classifier-empty | default",
  "requirements": {"matrix": {"debug": 3, "reasoning": 3},
                   "complexity_sig": "ab12…", "confidence": 0.8,
                   "prompt_version": "v1", "model": "glm-5.3-flash", "problems": []},
  "sort": "predicted_cost_per_task", "chain_length": 12, "max_hops": 3,
  "served_by": {"provider": "deepseek", "model": "deepseek-v4-flash"},
  "ladder": [{"hop": 2, "provider": "…", "model": "…", "usd_1m": 0.15,
              "stats_fallback": "unconditioned", "status": 200, "latency_s": 4.2,
              "outcome": "ok"}],
  "degrade_reason": null
}
```

**4. Semantics (spec R9–R11).**
- Transport failure OR non-2xx → the next hop is tried (breaker recorded per
  attempt), bounded by `max_hops`; when every hop fails the LAST upstream
  response is returned with `_router.exhausted: true` (never a fabricated 200).
- No open hop for the request → `503` with `_router.gate` naming the gate.
- **Content dissatisfaction is NOT a retry trigger** — the router does not
  silently re-ask a different model for a better answer.
- Every attempt is recorded as an outcome row (`source_system: router-proxy`),
  so the ladder's own performance feeds the averages it sorts by.
- `no open hop` / malformed classifier output never crash the request: the
  router returns a shaped error or the visible degrade, never a traceback.

**Streaming clients ARE served (TR-120, 2026-09-23):** the mirror BUFFERS —
`stream: true` is stripped from the forwarded hop, and the buffered completion
is re-served as one synthesized SSE completion (role + content + finish + usage
chunks, `[DONE]`). A hermes agent turn now runs through the mirror unmodified.

**TR-120 — Hermes client lane (VERIFIED live, proxy :9397).** Recipe + the two
failed paths + the shadowing root cause: see `docs/tr120-recipe.md` (short
version: name the custom provider `task-router` — `router` collides with the
built-in Ramp Router plugin — then `hermes chat -q ... --provider task-router
-m glm-5.3-flash` reaches the proxy and lands one attributed, metered outcome
row).

## Verification
- Tick spawns show the resolved model/provider in scheduler.log.
- Force a failure on a head pair → next spawn hops to chain hop 2; breaker file shows the open entry.
