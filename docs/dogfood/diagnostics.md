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
| `router validate: [FAIL] registry.exists: missing <data home>/registry.json` despite a seeded repo | (historic, TR-044-era) the CLI exported ROUTING_REGISTRY=<data home> over the scripts' repo default | FIXED TR-REV-1012-1: all schools resolve through scripts/registry_path.py (repo default; ROUTING_REGISTRY/TASK_ROUTER_HOME still honored) — if you still see this, check for a stale copy in ~/.hermes/scripts (run scripts/sync_runtime.sh) |
| server `/resolve` head ≠ CLI spawn head | server reads a different registry than spawn | (historic, same TR-044 family) FIXED TR-REV-1012-1 — both resolve the shared registry_path order; verify with `router status` `data_home.registry`, or pin both with an explicit ROUTING_REGISTRY |
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

## 2026-09-16 dogfood: the exit-code and data-home laws, re-measured

The second dogfood run (full report: `2026-09-16-integration.md`) re-tested
the same laws with cleaner methodology and found two records needed
corrections — a good example of why measurements should be repeated without
the original run's assumptions.

**Exit codes.** The table above says `router seed` without duckdb exits 0.
Wrong: re-measured without a pipe, `seed` **exits 1** ("router: dispatch
failed: No module named 'duckdb'"). The original reading was pipe-masked —
the `head` in the pipeline produced the 0. What DOES coerce to 0 is the
`task_router/cli.py` wrapper: a dispatch error (unknown subcommand, argparse
usage error) prints the child's usage and a "fail-open (coerced to 0)" line,
then exits 0 — for every subcommand, including ones with no fail-open
contract (`validate` documents "exit 1 + issues"). Lesson: the fail-open
contract belongs to runtime resolution (`spawn`), not to CLI misuse. Filed
as TR-058. When measuring exit codes, never let a pipeline's last command
stand in for the program under test.

**Data home.** TR-044 is still open and now has a one-line live repro: from
the repo cwd with no `TASK_ROUTER_HOME`, `router status` reports
`<repo>/registry.json` while `router spawn 9router` reports
`~/.local/share/task-router/registry.json` (fallback=true, bootstrap=true).
The wrapper only injects data-home env for spawn/circuit/ledger/maintain/
seed/gaps/pricing/modelsdev/clinepass/probefix; status/validate/estimate/
diff/metrics/server/web keep repo-relative defaults. Today the heads match
across surfaces only because every path derives from the same committed
tables — the moment one surface seeds fresher state, they diverge silently.
Filed as TR-056 with the bunker corollary TR-057: a fresh install that
follows the documented data-home law (`TASK_ROUTER_HOME=~/x router seed`)
passes spawn but FAILS `router validate`, which still checks
`<repo>/registry.json`. A user who does everything right gets a red
integrity check on a healthy install.

**xKiro economics proved end-to-end (TR-054 follow-through).** The chain now
carries plan-tier ordering on 115 imported lanes: `spawn 9router` heads with
`clinepass/stealth/union-alpha` @ $0.0, and the same head comes back from
the REST server, the MCP bridge (`tools/call` → `resolve`), and the web
preview (`/api/preview`) — four surfaces, one answer. The xKiro pair
breakers opened on real tick outcomes during the 09-16/17 upstream storm,
so the gate wiring has now reacted to genuine failures, not just synthetic
ones.

**Noise budget.** ROUTER-MISS stderr telemetry improved but is still the
dominant friction: 760 lines / 479 tier=None per ad-hoc resolve
(TR-055; 09-12 baseline 1004/724). The gates are correct — the noise is
telemetry policy, and flipping the default to quiet (verbose behind an env
flag) is a one-line change the foreman can make.

**Fresh install numbers (repeat of the bunker leg, new agent):**
clone 1s → `pip install -e .` 5s → duckdb 14s → seed 8s → first resolve.
Total ≈ 27s to a real 18-hop chain whose head matches production exactly.
The 09-12 conclusion holds on a second independent machine.

---

Not a log dump. This is **how the thing is built, why, what broke during the run,
and the right way to drive it** — written so the next agent (or a future chat)
can answer "is this project worth anything / does it actually work?" by reading
the repo instead of re-running the suite.

## 2026-09-20 dogfood: the data-home boundary, the proxy ladder, and the false-red validate

### How it is built (and why that shape)

```
profile / project ──► registry (registry.json, seeded from data/tables/*.jsonl)
                        │  eligibility: every signed requirement clears (+ min_context)
                        ▼
                  price-ordered eligible pairs   ← DETERMINISTIC: policy, not telemetry
                        │
                        ▼
                  runtime gates AT RESOLVE TIME: quota | health | circuit | diversity | ledger
                        │
                        ▼
        head + surviving chain + exclusions[].why   ← the audit trail
                        │
                        ▼
   caller spawns … and (the TR-067 path) calls the upstream gateway itself
```

The load-bearing design decision is **transient signals never rewrite the price
order** — they are applied at resolve time and reported as exclusions. That is
why a resolve can be reproduced and audited: the stored chain is policy, the
gates are stamped on top, and every drop carries a reason string.

Second decision: **the CLI is a dispatcher, not a program.** `task_router/cli.py`
`runpy`s `scripts/router_<name>.py` and injects env per subcommand
(`ROUTING_DATA_DIR`, `ROUTER_STATE_DIR`, `LEDGER_FILE`,
`ROUTING_NS`, … — ROUTING_REGISTRY deliberately NOT since TR-REV-1012-1: the
scripts' shared resolver scripts/registry_path.py owns registry resolution and
the export preempted operator overrides). Everything subtle about this project
lives in that table — a subcommand with the wrong exports reads a different
registry than `spawn` does (the TR-044/TR-056 defect class, both now verified
fixed). Two rows in the table
are **deliberately empty**, and both are the "wrong fix is worse" cases:

- `"quota": {}` — `router_quota.py` writes the gate where the **fleet spawn path**
  reads it (`~/.hermes/model-router/quota-state.json`, no `ROUTER_STATE_DIR` in the
  tick env). Exporting the data home here would make `router quota set` a silent
  no-op for the exact feature it implements (TR-060).
- `"probe": {}` / `"pricing-audit": {}` / `"outcomes": {}` — the calibration and
  audit tools intentionally read the **fleet** registry and the **live** meter
  (`~/.hermes/state.db`); a data-home export would point them at a fixture.

**Right way:** if you set `TASK_ROUTER_HOME` for scratch work, remember it scopes
*readers* and the circuit/ledger writers only. `quota` (and the calibration
commands) still write fleet state — pass `--state-file` to aim them at a scratch
file. This is the one place where "isolated scratch run" can touch production
(TR-083).

### How the run went (and the exact errors hit)

| # | Error string (verbatim) | Reading | Right way |
|---|---|---|---|
| 1 | `router: unknown command '--version'` | the CLI has no `--version`; the command list is the version surface | `router --help` |
| 2 | `router_circuit.py: error: unrecognized arguments: --provider --model --reason` (rc=2) | the documented invocation in `docs/soft-gate-integration.md` does not exist | positional: `router circuit record-failure <provider> <model> "<reason>" --class <class>` (TR-084) |
| 3 | `NOT WIRED: ledger.jsonl has no trace rows` (warning on every resolve) | TR-007's concurrency gate is inert until the *scheduler* calls `ledger start/end` | not a defect — a cross-repo integration gap, stated in the payload |
| 4 | `[FAIL] freshness: stale registry (warning-level): fallback_lanes.jsonl is 0s newer than registry.json` | `router seed && router validate` is red immediately after seeding (control host **and** fresh Debian 13) | TR-082 filed; until fixed, treat `validate`'s freshness line as advisory |
| 5 | `classifier call failed: ROUTER_CLASSIFIER_BASE_URL not configured` inside `degrade_reason` | the promised *visible* degrade worked; with the doc's `ROUTER_CLASSIFIER_KEY_ENV=ZAI_GLM_API_KEY` (absent from the fleet `.env`, which has `ZAI_API_KEY`) the same degrade would hit a wired classifier | check `degrade_reason` before trusting cost ordering |
| 6 | `{"error":"No active credentials for provider: zai-glm"}` (9router, 404) | probing the resolved `provider` id against the **9router** address space is the wrong layer — the router resolves *fleet* provider lanes, not 9router aliases | drive the router's own surfaces (CLI / REST / MCP / proxy); do not re-route its head through 9router by name |
| 7 | `COLLECT-FAIL unreachable-agent` for `agent=e3d3cd28` | the shared default evidence path held a **2026-09-18** run's meta; this launch wrote no `.meta` of its own | TR-085 filed against the harness — always pass `--evidence <per-run-path>` |

### The one P1 this run found, and how to see it again

`docs/integration.md` promises the proxy is a *ladder*: classify → chain → walk the
chain on transport failures, bounded by `x-router-max-hops` (default 3).
Measured live (200 OK, served by `zai-glm/glm-5.3`, 115 s):

```json
"_router": {"chain_length": 40, "max_hops": 2,
            "ladder": [{"hop": 4, "provider": "zai-glm", "model": "glm-5.3",
                        "usd_1m": 1.52, "status": 200, "outcome": "ok",
                        "latency_s": 114.024}],
            "served_by": {"provider": "zai-glm", "model": "glm-5.3"}}
```

One entry, and it is **hop 4** — the chain's hop 1 costs **$0.082/M**, i.e. 18.5×
cheaper, and was never attempted. The ladder is reported as if complete, so a
caller cannot tell a full walk from a single attempt (TR-081). **Re-run:**
`ROUTER_PROXY_UPSTREAM=http://127.0.0.1:8642 ROUTER_PROXY_AUTH=passthrough
router server --mode read-only --port 9193`, then POST
`/v1/chat/completions` with `x-router-max-hops: 2` and read `_router.ladder`.

### Environment traps that cost time

- **State-file keys.** The circuit/ledger state lived in `circuit-state.json` with
  the pairs under a top-level `pairs` key that also contains a `v2` sub-object.
  Top-level keys are **not** `$provider.$model`; read the file's own shape
  (`{version, pairs: {…, v2: {provider_breakers, classes}}}`) before parsing it.
- **Concurrency of the proxy.** A single upstream call takes ~114 s (gateway +
  large context). The server is `ThreadingHTTPServer`, so a bounded parallel
  probe is safe, but serial probes cost minutes each — budget for it.
- **The fresh box needs nothing special.** Debian 13 / Python 3.13.5, no clang,
  no cmake, no gcc, no `getfattr`: clone → venv → `pip install -e .` → `pip install
  duckdb` → resolver live in ~15 s. `python3-venv` supplies `pip` even though the
  base interpreter has no `pip` module.

### Where the value actually is

The honest answer to "does it work / is it worth anything":

- **It works, and its central artifact is the `exclusions[].why` trail.** A
  fleet operator can answer "why is my model X instead of Y" from one JSON
  payload, with the gate and its reset time quoted. That is the product.
- **It is trustworthy**: gates are honoured (breaker open → head moved; restore →
  head returned), state round-trips through the ledger, and it never corrupts
  anything it is asked about.
- **It is rough around its own edges**: two documented command forms do not
  execute, `validate` is red on a correctly-seeded fresh install, and the proxy's
  cost-saving promise fails silently in the direction that costs money.
- **Not verified here:** the classifier path itself (needs `ROUTER_CLASSIFIER_BASE_URL`
  + a key env name that exists) and the full 4-driver proxy integration suite (TR-071..075).

---

# Run 2026-09-25 — versioned/tagged profiles + provider-mapping rules (7th run)

# Task-Router Diagnostics — how the versioning and mapping layers actually work

(2026-09-25 dogfood, 7th run. Explains the machinery behind the two surfaces
probed, why they look the way they do, and the traps they hide.)

## How profile "versioning" is really built

Layer by layer:

1. `data/tables/task_profiles.jsonl` — one JSON object per profile row. The
   versioning convention is NOT `id:version` refs; it is **distinct ids
   sharing a tag** (`P3_DOCS` v1, `P3_DOCS_V2` v2, both `tag: P3_DOCS`).
   This matches `tests/test_spawn_board_profile.py::test_tag_declaration_
   resolves_to_the_version_row`.
2. `router seed` inserts them into a DuckDB table whose PRIMARY KEY is
   `(id)` only (scripts/router_seed.py:853) — so id must be distinct per
   version; a duplicate id is a seed error, not a version bump.
3. `task_profile_requirements.jsonl` keys requirements by `task_id` +
   `category` with PK `(task_id, category)` — **no version column**. A v2's
   stricter requirements are a new set of rows with the v2 task_id. This is
   why "version" is a naming convention, not a storage dimension: resolve()
   never sees a (profile, version) pair, only profile ids.
4. `resolve()` builds `profiles = {row['id']: row}` (id-keyed dict) and
   `_resolve_profile_tag()` first scans for `row['tag'] == ref` (picking the
   highest `version` among tag matches), THEN falls back to exact id.

The trap: because tag matching precedes exact-id matching and operates on
the same input string, **retagging to v2 shadows the v1 row's own id**.
`--profile P3_DOCS` after the retag resolves to `P3_DOCS_V2`. There is no
`P3_DOCS:1`/`@1` syntax (PROFILE_NOT_FOUND), so the README's "pinned old
versions still resolve by version" is unimplementable by any caller. The
version-desc sort in `_resolve_profile_tag` (router_spawn.py:863) is
effectively dead code: it orders rows that share a tag, but any caller who
could name both would already have two distinct ids.

Right way, today: if you need old-version resolution, do NOT reuse the old
id as the old tag. Give every version a unique tag (`P3_DOCS_V1`,
`P3_DOCS_V2`) and point callers at explicit tags — then nothing shadows.

## How provider mapping is really wired

`data/tables/provider_mappings.jsonl` rules are consumed by exactly two
code paths, and they do different things with the same file:

- `scripts/router_modelsdev.py` — applies rules to EXTERNAL models.dev
  catalog provider ids so the catalog sync can find our registry provider
  (`gw-foo -> foo`, `myrouter:zai-glm -> zai-glm`, plus ~100
  `modelsdev-silence` rules that mark catalog families as deliberately not
  imported). This path genuinely rewrites names.
- `scripts/router_seed.py` (:357-375) — after building the registry, it
  scans lane provider ids that are NOT canonical and prints either
  `mapping: external lane X -> canonical Y` or `GAP: ... visible gap`.
  **It only prints.** The models row keeps its external provider id, and
  since chains are built from registry rows, a renamed lane never routes
  unless a physical `providers.jsonl` row also exists for the external id.

Right way, today (proven this run): a renamed gateway lane that must route
needs BOTH (a) the mapping rule — for catalog sync and for the seed
reconciliation report to stay gap-free — AND (b) an actual row in
`providers.jsonl` carrying the external id. The rule alone is a report
line, not routing.

## The fail-open contract colors everything

Every caller-path error above surfaced as `{"error": ..., "code":
"PROFILE_NOT_FOUND"}` with **exit 0**. That is by design (the scheduler
must never block on routing infra), but it means a caller who checks only
exit codes will treat a mistyped version pin as success. Check `.head`
presence, not `$?`. This is documented in the usage skill; it bears
repeating because it turns "wrong pin syntax" from a loud failure into a
silent one.

## How this run was isolated (pattern for future runs)

`TASK_ROUTER_HOME` (state), `ROUTING_NS` (scratch DuckBrain ns),
`ROUTING_DATA_DIR` (copy of data/tables) redirect every writer. Nothing
under the repo or the live `~/.hermes` / DuckBrain tree was touched; the
one repo-visible side effect of `router seed` is the `synced data/tables`
step, which only syncs when the data dir is repo-owned.

## Run 31 (2026-10-01) — deployment drift is invisible to every guard that exists

The repo ships three drift defenses and none of them see the drift that
actually happened:

1. `sync_runtime.sh` keeps live installs canonical (symlinks for subprocess
   consumers, byte-copies for the cron runner). But nothing re-runs it on a
   schedule, and its byte-copy list has no verify mode — copy-list drift
   (TR-256: two of six files diverged BOTH ways, live older AND newer than
   repo) is only visible if you diff by hand.
2. `router_health.py` code_identity() (TR-141) makes the serving process
   compare its boot SHA against disk HEAD and report `stale: true`. Honest,
   and useless without a consumer: the :9092 instance has been stale since
   09-30 and no cron/watchdog reads it (TR-255).
3. `fleet-cooldown-policy.py`'s hash guard (SCHED-PERF-006) compares the
   live file to a sidecar hash — but the sidecar is updated by the same
   hand that edits the live file, so live-vs-REPO drift passes silently.

The right way (what the foreman should land): a `make verify-deploy` that
runs sync_runtime.sh in a check-only mode plus a curl of /health asserting
`code.stale == false`, both called from the existing hourly probe cron.

The honest way to prove which code serves you, learned from the 2026-09-25
proxy incident and re-proven today: ask the process, never the disk. The
proxy test plan's F-01 rule ("deploy = sync+restart; a pull is not a
deploy") needs its missing half: a restart is not a deploy either — the
receipt is the process's own boot identity matching HEAD.

## 2026-10-13 run 32 — intake/ingress/credentials angle

The two-plane state split is the recurring trap: registry lives under
TASK_ROUTER_HOME, gates/health/circuit under ROUTER_STATE_DIR (default
~/.hermes/model-router). Sandbox BOTH or resolves silently empty (TR-319).
The right way: export both, copy quota-state.json + circuit-state.json into
the scratch gate dir, then drive. Ingress debugging ladder that worked:
forward --message-file for the offline refusal ladder, serve for anything
that must accumulate state (circuit, admission), poll against the real bus.
Circuit state is per-process — never proof the breaker is broken from
repeated forward calls (TR-321). Install leg ran on bunker-mvp: fresh
clone → venv → pip -e → seed → spawn → validate 14/14 in 39.5s; spawn
161ms±15 warm — nothing slow enough to profile.

## 2026-10-16 run 34 — cache + quota angle

What the run taught about how the thing is built, in the order a user hits it:

1. **One install path, two servers.** The quickstart's `pip install -e .` +
   `router seed` covers everything; the two long-lived processes people run
   (`router server` :9092, `router web` :9093) share the data home through
   scripts/registry_path.py but do NOT share a route table — :9092 owns the
   TR-150 UI data plane, :9093 embeds its own older page and dispatch. The
   "same page, no second UI" comment in router_web.py is the drift vector;
   the fix direction is one served page module or an explicit proxy.
2. **The cache is invisible by design — verify it from /health.** TR-277
   routes reads through router_data_cache.cached_read_jsonl with a direct
   read as the fail-open fallback; a user cannot tell cache from no-cache
   by latency at this size (both ~10ms), only by the /health data_cache
   counters. The kill-switch equivalence (ROUTER_CACHE=0 producing an
   identical chain) is the real contract; test_cache_spawn_equivalence.py
   is where it lives.
3. **Fresh-machine suite failure is a two-line fix — but it was sitting
   uncommitted in the main tree for hours** (guarded import in
   router_maintain.py + module skip in test_pricing.py, .gitreins task
   in_progress). Diagnostic: simulate the fresh venv by blocking the
   duckdb import at builtins level and importing tests/test_pricing.py —
   the collection ERROR reproduces deterministically on unpatched HEAD.
4. **Live-state hygiene:** the state planes (~/.hermes/model-router for the
   fleet, ~/.local/share/task-router default) are shared across EVERY clone
   on the box. This run stayed scratch-clean (TASK_ROUTER_HOME default to
   the clone, no quota/circuit writes) — the working-tree mtimes at
   15:29-15:31 belong to the fleet's :9092 server and a sibling lane's
   guard run, verified by process census before attributing anything.


## 2026-10-19 run 36 — the quality artifact: one producer, no consumers, two numbers

**What the surface is.** `scripts/quality_artifact.py` (TR-281, landed `6a5d82e`) is the single
writer of `data/state/quality-artifact.json` — schema_version 1, `measured_at`, and one entry per
metric (`type_hint_pct`, `coverage_pct`, `wiring_pct`) carrying value/unit/stage/target/priority/
blocking/definition_cmd/breakdown, with `value=null` + a `reason` string whenever a metric cannot
be measured. It writes atomically (mkstemp + os.replace) and exits non-zero only when the artifact
cannot be produced. Its docstring states the intent: *"so gitreins, the board and the UI read the
same numbers ... every consumer reads data/state/quality-artifact.json and never recomputes a
metric."*

**How it is built (the honest parts).** `measure_type_hint_pct()` shells out to
`scripts/type_hint_score.py --json` and takes `overall.fn_score` (functions carrying a return
annotation / all functions). `coverage_pct` and `wiring_pct` are hard-coded unmeasurable with
reasons that name the pending rows (TR-187, TR-282) — a NULL with a reason, never a fake 0.
Running it twice on the same tree produces byte-identical output apart from `measured_at`, so the
committed artifact is reproducible, not stale-in-value.

**Why the numbers disagree (the trap).** The same metric name is produced by two verbs with two
scopes:

* artifact → `type_hint_score.py` → `scripts/` **plus every top-level package dir** (its overall
  row: 1596 functions, 834 return-annotated) → `52.2556`
* the quality ladder's own `definition_cmd`
  (`python3 scripts/quality_score.py --json --metric type_hint_pct`) → walks **only
  `--scripts-dir`** (default `<repo>/scripts`) and returns `return_pct` → `52.47`

Both are right for their own scope; the danger is that the ladder's stage/next_target (stage 1,
next 60.0) is computed from the second number while the artifact publishes the first. Whoever
wires the first consumer gets a value that disagrees with the ladder it is meant to grade.

**How to re-prove both, in two commands** (from a clean clone; no duckdb needed):

```bash
python3 scripts/quality_artifact.py --output /tmp/a.json && jq '.metrics[]|{metric,value,reason}' /tmp/a.json
python3 scripts/quality_score.py --json --metric type_hint_pct
```

**The gap that made this a finding.** Acceptance criterion 3 of TR-281 names its consumers
(gitreins GR-142/143, the board, the UI). A tracked-file grep finds none:
`git grep -c quality-artifact` = the script (3), its test (3), `.gitreins/tasks.yaml` (1: the AC
text), the artifact itself (1: its own writer module name). `.gitreins/config.yaml` carries no
quality guard; the CI workflow has no quality step; the Makefile has no quality target; the UI
server's only "quality" hits are the unrelated `quality_estimates` registry table. Rows:
`TR-1000976681059241` (P1 consumers), `TR-1000976681059242` (P2 two numbers),
`TR-1000976681059243` (P3 undiscoverable).

**Install/environment notes on the fresh box** (ephemeral `bunker-mvp` agent `df-tr-1019b`,
destroyed, 0 residue): clone `42e4e60` 2s; verbatim `pip install -e .` rc=1 with the PEP 668
`externally-managed-environment` message; venv rescue 10s; `pip install duckdb` 2s;
`router seed` 31s (writes registry.json, 1992 rows / 838 active / 29 providers); `router spawn`
resolves; the artifact verb rc=0 in 1s cold with the same 52.2556. The island between README line
69 (`pip install -e .`) and PEP 668 is real but one line of README (the venv) already rescues it.


## 2026-10-19b run 37 — how the circuit breaker decides its blast radius (and why one substring default is load-bearing)

`router_circuit.py` stores state for `(provider, model)` pairs and opens breakers with a flat per-class
cooldown (`CLASS_COOLDOWN_S`): `overload` 120s, `quota_window` 300s, `api_down` 1800s, `out_of_credit` 14400s.
Two blast radii follow from the class:

* **SOFT** (`overload`, `quota_window`) → the pair alone is gated for its short cooldown.
* **HARD** (`api_down`, `out_of_credit`) → the pair is gated for 30m/4h, and **three** HARD failures of the same
  class across any model of that provider open a PROVIDER-WIDE breaker that excludes every lane of it.

So the class is not bookkeeping — it is the difference between "one model rests for two minutes" and "a whole
provider leaves the fleet for half an hour". `_circuit_class(reason)` in `router_server.py:3372-3390` decides it
by substring: `timeout|timed out|idle|deadline|too slow` → `overload`; `429|quota|rate limit|rate-limit|
rate_limit` → `quota_window`; **everything else → `api_down`**. The proxy's failed-hop path
(`router_server.py:4222-4224`) passes the upstream payload's own `error` text (first 200 chars) as the reason.

Consequence, measured (14 shapes): 10 land HARD, including router-generated text — `no open hop for this
request`, `503 no open hop for this request`, `502 bad gateway`, `no-hops`, `unservable-rating`,
`registry-missing`, and the empty string. Only rate-limit-shaped and timeout-shaped text earns the soft class.

Two guards already exist and must not be mistaken for the whole fix: `route_outcome in ('rejected','no-hops')`
and a `provider == 'none'`/`_NOT_A_LANE` identity filter (`router_server.py:3592-3606`) keep the router from
opening a breaker **under a fake identity** — verified live (`record-failure none none …` →
`SKIPPED none/none …`, state file unchanged). They do nothing about the same text landing on a REAL pair, which
is exactly what probe 3 shows.

**Re-prove in three commands** (all read-only against a scratch state dir; never point these at the live
`~/.hermes/model-router`):

```bash
export ROUTER_STATE_DIR=$(mktemp -d)
python3 scripts/router_circuit.py record-failure none none "no open hop for this request"   # SKIPPED
python3 scripts/router_circuit.py record-failure <provider> <model> "no open hop for this request"  # OPEN, api_down
python3 scripts/router_circuit.py status --json                                            # read the class back
```
