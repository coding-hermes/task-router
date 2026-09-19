# Plan-window quota gates (TR-060)

A subscription lane that answers **429 plan-limit** is not a blip — it is dead
for hours or days. The circuit breaker cannot express that: `router_circuit.py`
cools `api_down` in 30 min, so the resolver re-picked the exhausted lane on the
next tick, it re-failed, and every affected fleet session fell back to the PAYG
`deepseek` default.

Live audit (2026-09-17, 24h window, `~/.hermes/scripts/routing-billing-guard.py`
+ `agent.log` correlation):

- 276 `api_server` sessions billed `provider='deepseek'`; 276/276 explained by a
  gateway `Fallback activated: <lane> → deepseek-v4-flash (deepseek)` line
  (0 unexplained) — i.e. the request reached the intended lane and the gateway
  fell back **by design**. The waste is on the router side: the exhausted lane
  was re-picked as head ~200 times.
- The two 429 shapes, verbatim:
  - OpenAI / openai-codex — `{'type': 'usage_limit_reached', 'message': 'The
    usage limit has been reached', 'plan_type': 'prolite', 'resets_at':
    <epoch>}`;
  - zai-glm — `{'code': '1310', 'message': 'Weekly/Monthly Limit Exhausted. Your
    limit will reset at 2026-09-20 04:07:32'}`.

TR-060 records that plan window as a **state gate with an expiry**, so the head
advances to the next eligible lane and the gate frees itself when the plan
refills.

## State shape

`<state dir>/quota-state.json` — the same file the resolver reads:

```json
{
 "updated": "2026-09-19",
 "providers": { "zai-glm": {"status": "open"}, "...": {"status": "open"} },
 "quota_exhausted": {
  "zai-glm": {
   "status": "gated",
   "reason": "HTTP 429 code 1310: Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-09-20 04:07:32",
   "reset_at": "2026-09-20T04:07:32+00:00",
   "detected_at": "2026-09-19T05:50:47+00:00"
  }
 }
}
```

A **nested** spelling is honored as well (merged over the top-level section), so
a hand-written gate lands wherever the operator looks:

```json
"providers": {"zai-glm": {"status": "open",
  "quota_exhausted": {"status": "gated", "reason": "...", "reset_at": "..."}}}
```

| field | meaning |
|---|---|
| `status` | `gated` (default when absent). `open`/`cleared`/`expired` = not gated. |
| `reason` | goes verbatim into the gate reason shown in `gate_reasons[]`. |
| `reset_at` | ISO-8601 (tz optional → UTC) or, when pasted from a provider body, epoch seconds. A provider-quoted wall clock ("reset at 2026-09-20 04:07:32") carries **no timezone**: it is read as UTC unless the operator says otherwise, i.e. the gate errs on holding the lane a little too long rather than re-picking a lane that is still dead. |
| `detected_at` | when the 429 was observed (audit only). |

## Semantics

- **GATED** while `status` is gated-ish **and** (`reset_at` is in the future
  **or** missing/unparseable). A gate with no usable reset time stays gated —
  the router never silently re-picks a lane it was told is dead.
- **OPEN (auto-clear)** as soon as `reset_at` passes. The entry stays in the
  file for audit and is reported under `quota_gates.expired`; **nothing has to
  be edited** for the lane to come back.
- The gate applies to the whole provider (quota is per plan), is checked in the
  primary chain **and** in the fallback-lane path (a quota-dead provider must
  not come back as the fallback head), and emits:

  ```
  quota exhausted: <reason> (resets <reset_at>)
  quota exhausted: <reason> (no reset time)
  ```

- `providers.<p>.status != 'open'` is the **other**, older mechanism: a policy
  gate with no expiry (grok-build, crof, aws-bedrock …). `router quota clear`
  never touches it.
- Fail-open is unchanged: an absent/unreadable/malformed document gates
  nothing and never raises (AGENTS.md).

## Where the file lives

| invocation | state dir | file |
|---|---|---|
| scheduler tick (`~/.hermes/scripts/router_spawn.py <project>`) | script default `~/.hermes/model-router` | `~/.hermes/model-router/quota-state.json` |
| `router spawn` / `router status` (console script) | data home (`TASK_ROUTER_HOME` > `XDG_DATA_HOME/task-router` > `~/.local/share/task-router`) | `<data home>/quota-state.json` |
| `router quota …` | **script default** — deliberately *not* the data home | `~/.hermes/model-router/quota-state.json` |

`router quota` keeps the script default on purpose: the scheduler invokes the
script directly, so a data-home redirect would write a gate the fleet never
reads (see the comment on the `quota` entry in `task_router/cli.py`). Use
`--state-file`/`--state-dir` to target any other file.

## CLI

```bash
router quota set zai-glm "Weekly/Monthly Limit Exhausted (resets 2026-09-20 04:07:32)" \
    "2026-09-20T04:07:32+00:00"          # or an epoch: 1789805473 (OpenAI resets_at)
router quota set openai-codex "usage limit has been reached" 1789805473 --detected-at 2026-09-19T02:51:32+00:00
router quota status [<provider>] [--json]
router quota clear <provider> | --all
```

- `set` validates `reset_at` **before** writing: a malformed value exits 2 and
  leaves the file untouched (a typo must never create a permanently gated
  lane). A past `reset_at` is recorded and flagged as already auto-cleared.
- Writes are atomic (temp + fsync + `os.replace`) under an advisory `flock`,
  read-modify-write: `providers`, `diversity`, `models`, `note` survive
  byte-for-byte.
- Exit codes: `0` ok, `2` usage/validation (operator error, never masked),
  `1` unexpected failure with a one-line stderr message.

## Reading the result

`router spawn <project> --format json` now carries:

```json
"quota_gates": {
 "source": "/home/<user>/.hermes/model-router/quota-state.json",
 "gated":   [{"provider": "zai-glm", "status": "gated", "reason": "…",
              "reset_at": "2026-09-20T04:07:32+00:00",
              "detected_at": "2026-09-19T05:50:47+00:00",
              "gate_reason": "quota exhausted: … (resets 2026-09-20T04:07:32+00:00)"}],
 "expired": [{"provider": "openai-codex", "reset_at": "2026-09-19T08:11:13+00:00", "…": "…"}]
}
```

`gates_loaded` is deliberately **unchanged** (TR-025 pins that dict), and every
excluded hop still appears in `exclusions[]`/`gate_reasons[]` with the
`quota exhausted:` reason — an invisible gate would be a silent zero-chain.

## Verification

```bash
# 1. the gate is in effect and the head moved
router spawn <project> --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); \
  print(d["head"], d["quota_gates"])'
# 2. no PAYG fallback for the gated lane during its window
grep -c "Fallback activated: glm-5.3-flash → deepseek-v4-flash" ~/.hermes/logs/agent.log
# 3. the regression battery
python -m pytest -q tests/test_quota_gate.py
```

Related: `docs/soft-gate-integration.md` (429 `quota_window` handling on the
caller side), `scripts/router_circuit.py` (30-min `api_down` breakers — unchanged
for genuine 5xx), `~/.hermes/scripts/routing-billing-guard.py` (the audit that
measures the fallback rate this gate removes).
