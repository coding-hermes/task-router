# task-router diagnostics — how it is built, what breaks, the right way

Explained trail, not raw logs. Read this before debugging router behavior.

## Architecture in one paragraph

The router is a registry + resolver + gates. **Tables** in `data/tables/*.jsonl`
(git-tracked, generated — never hand-edit; rebuild via `router seed`) describe
providers, models, plan terms, capability tiers (`model_tier`: one row per
(model, category) with a signed −5..+5 level), benchmark-derived performance,
and aliases (`model_aliases`: vendor/HF/snapshot ids → canonical weights so
tier inheritance works). `router seed` compiles these (duckdb SQL) into
`registry.json` — a single JSON document with `tables: {name: [rows]}`.
`router spawn` loads `registry.json`, filters every (provider, model) pair by
the task profile's per-category requirements (+ `min_context`), sorts the
survivors by plan tier then effective price, then applies runtime gates:
quota state (`quota-state.json`), health, circuit breakers
(`circuit-state.json`), diversity, and the concurrency ledger. The output is
head + full chain + explicit exclusions + provenance (`source`,
`fallback_used`, `warnings`).

## The four data locations (and how they interact)

1. **repo `data/tables/`** — canonical seed input, git-tracked.
2. **`registry.json`** — gitignored build artifact, written by `router seed`
   next to the tables (repo root by default, or `$ROUTING_REGISTRY`).
3. **data home** (`TASK_ROUTER_HOME` > `XDG_DATA_HOME/task-router` >
   `~/.local/share/task-router`) — where the installed CLI puts state
   (`quota-state.json`, `circuit-state.json`, `ledger.jsonl`,
   `metrics.jsonl`) and, after `router seed`, `registry.json`.
4. **`~/.hermes/model-router/`** — legacy global state dir; some scripts
   still read health state from here regardless of the data home.

**Pitfall (TR-044):** only `spawn`, `circuit`, `ledger`, `maintain`, `seed`,
`gaps`, `pricing`, `modelsdev`, `clinepass` receive data-home env exports
from `task_router/cli.py`. `validate`, `status`, `estimate`, `diff`,
`metrics`, `server`, `web` silently fall back to repo-relative or global
paths. If two router surfaces disagree about the chain, check which registry
each one loaded (`source` + `fallback_used` in spawn output; `registry.error`
in status output).

**Pitfall (TR-045):** a scratch `router seed` used to export into the live
DuckBrain mirror (`ROUTING_NS` was hardcoded to the fleet mirror path). Since
cbc3075 the `router` CLI exports `ROUTING_NS=<data home>/ns/routing` for the
seed subprocess when a data home is in play (setdefault semantics — an
explicit `ROUTING_NS` still wins), and `router_maintain.py` setdefaults its
seed child the same way, so scratch/data-home seeds stay self-contained and
never write the live fleet mirror. Remaining edge: direct invocation of
`scripts/router_seed.py` with `ROUTING_NS` unset is its own guard (TR-048,
a977fa4) — it resolves under the data home's scratch ns, and the fleet
mirror is only reached via explicit `ROUTING_ALLOW_FLEET_MIRROR=1`; set
`ROUTING_NS` explicitly to place direct-script scratch runs anywhere else.

## Errors hit during the 2026-09-12 dogfood run, and their meaning

| Symptom | Root cause | Right response |
|---|---|---|
| `router: dispatch failed: No module named 'duckdb'` | seed needs duckdb; pyproject declares stdlib-only runtime and README omits it | `pip install duckdb` (or make the import lazy — task for the foreman) |
| `ROUTER-MISS: <provider/model> fails <cat>>=<n> (tier=None)` × ~1000 per resolve | per-(lane × requirement) exclusion telemetry; `tier=None` = no model_tier rows reached the resolver for that lane (alias inheritance not consulted on the spawn lookup path) | filter with `--quiet`; if head looks wrong, check `exclusions` in the JSON — the gates themselves are working correctly |
| `router validate: [FAIL] registry.exists: missing <repo>/registry.json` despite a seeded data home | validate reads `ROUTING_REGISTRY` but the CLI never exports it | seed into the repo root (`ROUTING_REGISTRY=$PWD/registry.json router seed`) or await TR-044 fix |
| server `/resolve` head ≠ CLI spawn head | server reads a different registry (repo-relative) than spawn (data home) | same registry for both: run both with the same explicit `ROUTING_REGISTRY`, or fix TR-044 |
| `WARN degraded_fallback` / `fallback_used: true` | registry.json missing/corrupt → resolver used committed `data/tables` (fresh-clone stdlib path) | run `router seed`; the fallback is deliberate design, not an error |

## How the circuit-breaker workflow is meant to be used

Callers record outcomes after every real spawn:
`router circuit record-failure <provider> <model> --class <api_down|out_of_credit|quota_window|overload>`
and `record-success` on recovery. Soft classes cool down fast (overload 2m,
quota window 5m); hard classes long (provider down 30m, out-of-credit 4h);
three same-class hard failures across a provider open a provider-wide
breaker. Verified end-to-end on 2026-09-12: two overloads → OPEN → next
resolve demoted the head and listed the pair in `exclusions` with
`circuit OPEN until …` → record-success → CLOSED → head restored. This is
the integration point the scheduler uses (via `~/.hermes/scripts/router_spawn.py`
subprocess, TASK-ROUTER-001/002).

## Repo-history lessons (why things are the way they are)

- **Fail-open is sacred:** `router_spawn.py` always exits 0; any internal
  error becomes `{"error": ...}`. The scheduler must never block on routing.
  The CLI layer extends this: dispatch errors in fail-open subcommands
  (spawn/probefix/plan-sweep) coerce to exit 0.
- **JSONL tables are generated.** Hand-edits get clobbered by the next seed;
  there is even a committed `.bak` (`fallback_lanes.jsonl.bak-20260901-131034`)
  from the era before that rule hardened.
- **The `TR-025` provenance fields** (`source`, `fallback_used`, `warnings`)
  exist precisely because the multi-location data model above used to make
  "which registry am I looking at" unanswerable. Read them before debugging.
- **2026-09-12 TR-039 (dda72a4)** added 137 model aliases + 178 neutral
  quality estimates to kill ROUTER-MISS noise at the seed layer — but the
  spawn lookup path still reports `tier=None` for the same ids (TR-043).
  Lesson: a fix measured at the seed layer must be re-measured at the
  consumer layer.

## What a fresh machine proved (installability)

Python 3.13, bare Debian user (no sudo, no toolchains): clone →
`python3 -m venv && pip install -e .` → 5s → resolve works off committed
`data/tables` with zero network calls beyond the clone. The stdlib-only
runtime design is real; only `seed` (duckdb) and tests (pytest) need extras.
