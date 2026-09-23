# task-router

A deterministic model router for AI fleets: task profiles → eligible provider/model pairs → price-sorted fallback chains → runtime gates.

`task-router` makes model selection inspectable rather than incidental. A task declares signed capability requirements by category. The router admits only pairs that clear **every** requirement, sorts the eligible pairs by effective price, then filters that stored chain with current quota, health, circuit-breaker, diversity, and (when wired) concurrency state. The caller receives the first open pair and the full explanation of exclusions.

The runtime contract is **fail-open**: `router_spawn.py` always exits `0`. If resolution fails, it writes a JSON error so the consuming scheduler or application can use its own fallback instead of being blocked by routing infrastructure.

## Contents

- [Quick start](#quick-start)
- [The router command](#the-router-command)
- [Web UI](#web-ui)
- [API server and MCP bridge](#api-server-and-mcp-bridge)
- [Classified proxy deployment (TR-067)](#classified-proxy-deployment-tr-067)
- [Architecture](#architecture)
- [Context windows and capabilities](#context-windows-and-capabilities)
- [Versioned, tagged profiles](#versioned-tagged-profiles)
- [Provider mapping](#provider-mapping)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Metrics](#metrics)
- [Data and namespaces](#data-and-namespaces)
- [Contributing and security](#contributing-and-security)

## Quick start

```bash
git clone https://github.com/coding-hermes/task-router.git
cd task-router

# Install the CLI (Python 3.11+; stdlib-only runtime).
pip install -e .

# `router seed` is the one command that needs a third-party package: duckdb
# (used as an in-memory engine — nothing binary is persisted). Every other
# subcommand runs on the stdlib alone.
pip install duckdb          # or: uv pip install duckdb

# Resolve a configured project to its gated, price-ordered chain.
router spawn my-project --format json

# A bare PROFILE name works in the project slot too: `router spawn P1_CODING`
# resolves exactly like `router spawn --profile P1_CODING`, and
# `router estimate P1_CODING` prices that same chain.
router spawn P1_CODING --format json

# One-command overview of registry, gates, circuit, and gaps.
router status

# Run the test suite (748 tests). The suite imports duckdb too, so run it with
# an interpreter that has duckdb — the board venv on the fleet hosts:
#   ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/
python3 -m pytest -q tests/
```

Skipping the `duckdb` step is not fatal: only `router seed` fails, and it fails
loudly (`router: dispatch failed: No module named 'duckdb'`) instead of writing
a partial registry; the test suite needs it too (see above). Every other
subcommand works without it.

On first run the CLI creates a data home and bootstraps a starter state file
(`quota-state.json`) with every provider explicitly **open** — you get a
visible, editable policy file instead of a silent zero-chain surprise. Edit it
to gate providers; delete it and spawn fails closed (nothing resolves) rather
than guessing.

Useful read-only checks:

```bash
router spawn --list-profiles
router spawn --profile-req 'reasoning=5 debug=3 min_context=100000' --format json
router circuit status --json
router validate            # integrity check; exit 1 + issue list when broken
router estimate --project my-project --tokens-in 100000 --tokens-out 100000
# same input positionally (a profile id/tag works here too)
router estimate my-project --tokens-in 100000 --tokens-out 100000
router diff 2026-08-31 2026-09-01
```

### Data home

State and runtime data resolve in this order:

1. `TASK_ROUTER_HOME`, when explicitly set;
2. `$XDG_DATA_HOME/task-router`; otherwise
3. `~/.local/share/task-router`.

`registry.json`, circuit state, the spawn ledger, health state, and metrics
all resolve under the data home when driven through the `router` command.
Running `python3 scripts/<tool>.py` directly keeps the historical
repository-relative defaults.

**Not every writer is data-home scoped.** A few commands read and write the
*deployment's* state locations by design, because the fleet's own spawn path
reads them there:

- `router quota` / `router quota set|clear` — writes the quota-gate file
  (default `~/.hermes/model-router/quota-state.json`, or `ROUTER_STATE_DIR`).
  Setting `TASK_ROUTER_HOME` does **not** redirect it.
- `router probe`, `router probefix`, `router plan-sweep` — resolve the fleet
  registry/state locations for the same reason.

To gate a lane against a scratch file instead of production policy, point the
state directory at a throwaway location explicitly (`ROUTER_STATE_DIR=/tmp/…`,
or the command's own `--state-file` where it offers one) — and remember the
default ports (`:9092` API, `:9093` web) are shared with a running fleet
instance, so use `--port` when probing by hand.

## The router command

`pip install -e .` installs one executable, `router`, with a subcommand per
tool. `router <cmd> --help` passes through to the underlying tool's own
argparse. Full table in [CLI reference](#cli-reference).

## Web UI

```bash
router web            # http://127.0.0.1:9093 — read-only by default
```

- **Preview pane** — pick a project or profile and resolve it: the real chain
  (hops, prices, context windows, exclusion reasons) as the router would pick
  it. Preview subprocesses the real resolver, so preview == reality.
- **Settings pane** — providers (plan, quota, enable/disable), profiles, and
  discounts, backed by the committed data tables with atomic, backed-up JSONL
  edits.
- **Auth** — reads are always allowed locally; writes are rejected in
  read-only mode (HTTP 403). Launch edit mode with `ROUTER_EDIT_API_KEY` set;
  the UI then asks for the key once and sends it as `X-API-Key` (missing or
  wrong key: HTTP 401). Edit mode refuses to start without a key configured.

## API server and MCP bridge

```bash
router server --mode read-only            # default; port 9092
ROUTER_EDIT_API_KEY=... router server --mode edit
```

- `GET /openapi.json` — the OpenAPI 3.1 description of every endpoint.
- Reads (all modes): `/status`, `/resolve?project=X`, `/profiles`,
  `/providers`, `/circuit/status`, `/gaps`, `/pricing`, `/chains`.
- Writes (edit mode + `X-API-Key` header only): `POST /circuit/record`,
  `/ledger/start`, `/ledger/end`, `/listings/provider|model|profile`.
  Read-only mode answers mutations with `403`; edit mode without the right
  key answers `401`. Every handler failure returns `{"error": ...}` — the
  server keeps running.
- **MCP bridge** — `POST /mcp` speaks JSON-RPC 2.0 (`initialize`,
  `tools/list`, `tools/call`). Tools are derived mechanically from the
  OpenAPI schema, so an MCP client can list and call the same operations —
  including gated mutations — under identical auth rules.

## Classified proxy deployment (TR-067)

The server also speaks `POST /v1/chat/completions` and `POST /v1/responses`
(Path B): it classifies the request (or takes the caller's declared
profile), builds the chain internally, walks a bounded fallback ladder
against the upstream gateway, and returns the upstream response shape plus
additive `_router` metadata (`complexity_source`, ladder trail,
`served_by`, skip/exclusion counts). `ROUTER_PROXY_AUTH=passthrough` lets a
caller's own upstream credential gate the mirror paths instead of the
router key — the gate is never weakened silently; without it the usual
router auth applies.

### Verified recipe (live-tested 2026-09-21 on :9391, real requests)

```bash
set -a; source ~/.hermes/.env; set +a
export ROUTER_PROXY_AUTH=passthrough
export ROUTER_PROXY_UPSTREAM=http://127.0.0.1:8642
export ROUTER_CLASSIFIER_BASE_URL=http://127.0.0.1:8642/v1
export ROUTER_CLASSIFIER_MODEL=deepseek-v4.1-flash
export ROUTER_CLASSIFIER_KEY_ENV=API_SERVER_KEY
scripts/router_server.py --mode read-only --host 127.0.0.1 --port 9391
```

Same process env covers the whole path: the resolver chain, the classifier
and the outcome/breaker bookkeeping all run as subprocesses of the server
(`env=os.environ.copy()`), so exported `ROUTER_*` variables propagate — no
per-tool config needed.

Env knobs:

| Variable | Purpose |
|---|---|
| `ROUTER_PROXY_AUTH=passthrough` | mirror paths gated by the caller's upstream credential |
| `ROUTER_PROXY_UPSTREAM` | upstream gateway base (default `http://127.0.0.1:8642`) |
| `ROUTER_PROXY_MAX_HOPS` | ladder bound; per-request `x-router-max-hops` wins |
| `ROUTER_SCORER` | default scorer: `classifier` (default), `jev`, `decisions` |
| `ROUTER_CLASSIFIER_BASE_URL/_MODEL/_KEY_ENV` | classifier endpoint, model id, env name of its key |

Caller headers: `x-router-scorer` (per-request scorer override),
`x-router-caller` (driver attribution), `x-router-profile` (declare the
complexity instead of classifying), `x-router-max-hops`, `x-router-sort`,
`x-router-window-h`, `x-router-developer-role: preserve`.

### Port collision check (2026-09-21)

All listeners enumerated with `ss -tlnp` before binding:

- `9092` — live fleet API server (`router server`, pid confirmed); proxy
  must never take it.
- `8642` — the Hermes gateway itself; proxy's upstream, never a bind
  target.
- `9292` — free at deployment time (it had been squatted by a scratch
  process during the 2026-09-20 verification, so re-check before every
  bind).
- `9391` — free; chosen for verification, and the verified recipe above
  pins it. Check before rebinding: `ss -tlnp | grep -E ':(9391|9292)\b'`
  (empty = free).

### Classifier lane health — read before pointing at the gateway

The classifier is a plain chat call to `ROUTER_CLASSIFIER_BASE_URL`, so it
is only as disciplined as the lane serving it. Live finding
(2026-09-21): identical calls with the classification prompt returned the
expected JSON object roughly half the time when pointed at the gateway;
`state.db` `session_model_usage` showed **zero** `deepseek-v4.1-flash`
rows in the window — the label rode gateway fallback hops that ignore the
classifier's system prompt and answer the raw task instead
(`complexity_source` then degrades to `default`, visibly, by design).
Called directly (`https://api.deepseek.com/v1`), the same lane classified
3/3. So:

- Prefer the deepseek endpoint for the classifier; treat
  `ROUTER_CLASSIFIER_BASE_URL=http://127.0.0.1:8642/v1` as a fallback to
  re-verify when gateway lanes move. Sanity-check before/after any
  restart:

  ```bash
  python3 scripts/router_classify.py --text "Write a quick file-copy script"
  ```

  JSON object with `categories` = lane healthy; anything else = degraded.

- Parity is defined on answer CONTENT, not generation identity: the proxy
  forwards the body unchanged and returns the upstream payload with
  additive `_router` fields, but the gateway may serve any lane for the
  requested model. Exact-match parity tests must pin an answer
  (constrained prompt) or compare the served model id; free-form prompts
  differ word-by-word across calls by nature.

### Client cutover (owner's call)

Point fleet clients' `base_url` at the proxy instead of the gateway:

```text
before:  http://127.0.0.1:8642/v1
after:   http://127.0.0.1:9391/v1      (same auth header works: passthrough)
```

Nothing else changes for callers — same wire shape, same key; responses
gain the additive `_router` envelope. The operator-facing flip (which
clients move, and when) is deliberately left to the owner; this repo ships
the recipe, the port checks, and the verified acceptance evidence, not
the fleet-wide reconfig.

### The :9391 instance is a managed unit (TR-087b, 2026-09-22)

The second listener on `127.0.0.1:9391` was previously a bare child of
`systemd --user` with no unit file — a control plane nothing could
health-check. It is now `task-router-proxy.service` (enabled,
`Restart=on-failure`), same verified recipe; the classifier credential lives
in `/home/kara/.config/systemd/user/task-router-proxy.env` (mode 600, never
in the unit file). Health is queryable like the :9092 server:

```bash
systemctl --user status task-router-proxy.service
curl -s http://127.0.0.1:9391/health
```

## Architecture

```text
configured project or ad-hoc requirements
                 |
                 v
     task profile (signed levels by category, versioned + tagged)
                 |
                 v
registry.json / committed JSONL tables
  eligibility: every category requirement clears (incl. min_context)
                 |
                 v
eligible pairs sorted by plan tier + effective price
                 |
                 v
runtime gates: quota | health | circuit (pair + provider) | diversity | ledger
                 |
                 v
head pair + surviving chain + explicit exclusions
                 |
                 v
caller spawns, records outcome, and may advance to next hop
```

### Deterministic eligibility and ordering

A profile is a set of requirements such as `reasoning=5 debug=3 vision=-2`
plus an optional `min_context` token floor. A provider/model pair is eligible
only if its signed tier is at least the required level for **all** categories
and its context window can satisfy `min_context` (pairs with an unknown
window pass but are flagged). The chain is sorted by plan tier and effective
price. Transient signals never rewrite the price order — they are applied at
resolve time and reported as exclusions, which keeps policy and operations
auditable.

### Circuit breakers (v2)

Failures carry a class: `api_down`, `out_of_credit`, `quota_window`,
`overload` (record with `router circuit record-failure ... --class <class>`).
Model-level breakers open on consecutive failures of one pair; soft classes
use short cooldowns (overload 2 min, quota window 5 min), hard classes long
ones (provider down 30 min, out of credit 4 h). Three same-class hard
failures across a provider's models open a **provider-level** breaker that
excludes every lane of that provider. Cooldowns are overridable with
`ROUTING_CIRCUIT_COOLDOWN_JSON`.

### Soft concurrency gate (default OFF)

When the scheduler wires `router ledger start/end` around spawns, the ledger
counts in-flight requests per pair. The strict gate is opt-in: set
`"soft_gate": true` in `quota-state.json` to exclude pairs at their
configured concurrency limit. **Off (default), a busy count never rejects a
request.** Either way, a real 429/overload failure records a short-cooldown
breaker so the next resolve advances to the next chain entry instead of
retrying the same lane. See `docs/soft-gate-integration.md` for the caller
contract.

### Fail-open contract

The router must not become a single point of failure. `router_spawn.py`
serializes failures as `{"error": ...}` and exits successfully. Consumers
must treat a missing head or error payload as a signal to use their
independently configured fallback, while logging the routing condition for
diagnosis.

## Context windows and capabilities

Model rows carry `context_limit` (tokens), `api_type`, `vision`, and
`thinking`, seeded from models.dev where the provider maps; unknown values
stay `NULL` with a note in `model_notes.jsonl` — never guessed. Seed
round-trips them into `registry.json`, and every chain lane reports its
context window.

## Versioned, tagged profiles

Profiles are versioned like container images: the row identity is
`(id, version)` and the human handle is `tag`. Profile references resolve
**tag first**, then exact id (so existing names like `P0_FORE` keep working).
Moving a tag to a newer version is a data edit — callers following the tag
pick up the new version without code changes, while pinned old versions still
resolve by version. Retagging is idempotent.

## Provider mapping

`data/tables/provider_mappings.jsonl` holds rename rules applied to external
provider names before registry matching — prefix strips (`myrouter:zai-glm`
→ `zai-glm`), regex rewrites (`^gw-(.+)$` → `$1`), and literal replacements.
First match wins; rules live in data, never in code. `router modelsdev sync`
imports catalog models for enabled providers only (enabled = not archived and
present in the catalog) and reports skipped and unmapped providers visibly.

## CLI reference

`router <cmd>` everywhere; the `scripts/router_*.py` names are the same tools
for repo-relative use.

| Command | Purpose |
|---|---|
| `router spawn` | Resolve a project or ad-hoc capability profile into a gated fallback chain. Flags: `<project>`, `--profile`, `--profile-req`, `--list-profiles`, `--explain`, `--format`, `--no-health`. A profile id/tag passed as `<project>` resolves as that profile (TR-059) |
| `router status` | One-command overview: registry source/freshness, health, quota, circuit, in-flight, gaps (`--format json\|text`) |
| `router estimate` | Cost preview for a project's chain at given token volumes (`<project>` positional or `--project`; profile id/tag accepted), head + top alternates, PAYG vs subscription annotated |
| `router diff` | Chain snapshot diff between two dates: head moves, new/dropped lanes, price deltas |
| `router validate` | Integrity check: registry schema/freshness, state files, profile integrity (`--json`; exit 1 on issues) |
| `router circuit` | Circuit breakers: `record-failure` (`--class`), `record-success`, `status`, `clear` |
| `router ledger` | Spawn lifecycle: `start`, `end`, `status` (in-flight counts, trace ids) |
| `router metrics` | Usage counters: `--top-providers`, `--top-models`, `--top-pairs`, `--profile`, `--since`, `--json` |
| `router seed` | Rebuild `registry.json` from committed tables (requires `duckdb`; derives `ROUTING_NS` under the data home) |
| `router modelsdev` | models.dev sync: `fetch`, `sync` (`--all` to include disabled; `--dry-run`), `mappings` |
| `router pricing` | Price table diagnostics (`--json`, `--dry-run`) |
| `router gaps` | Registry data-quality gaps (`--json`, `--lacking`, `--top`) |
| `router maintain` | Reprice / seed / export / snapshot / commit maintenance (`--dry-run`) |
| `router probe` | Provider health probe (`--only <provider>`, `--no-write` / `--dry-run`) |
| `router probefix` | Repair 404/400 model ids from probe logs |
| `router clinepass` | Cline Pass catalog sync (`--dry-run`, `--commit`) |
| `router plan-sweep` | Report/disable lanes outside flat plans (`--apply`) |
| `router learn` | Learning loop: `dump`, `lesson`, `doctrine`, `provider` |
| `router web` | Local web UI on :9093 (settings + resolve preview) |
| `router server` | OpenAPI API server + MCP bridge on :9092 (`--mode read-only\|edit`, `--port`) |

Commands that commit, push, write state, or change provider configuration are
operational tools — review `--help` and use `--dry-run` where available.

## Configuration

| Variable | Used by | Effect |
|---|---|---|
| `TASK_ROUTER_HOME` | the `router` CLI, metrics | Data home root; state + metrics resolve under it. |
| `ROUTING_REGISTRY` | spawn, seed, maintain, validate | Path to `registry.json`. |
| `ROUTING_DATA_DIR` | seed, spawn, pricing, modelsdev, clinepass, gaps, maintain | Directory of committed JSONL tables. |
| `ROUTER_STATE_DIR` | spawn, circuit, ledger, probe | Directory holding quota/health/circuit state and the ledger. |
| `LEDGER_FILE` | ledger, spawn | Exact ledger JSONL path (shared contract). |
| `ROUTER_EDIT_API_KEY` | server, web | Edit-mode API key; unset = read-only. |
| `ROUTING_CIRCUIT_COOLDOWN_JSON` | circuit | JSON patch overriding per-class cooldown seconds. |
| `ROUTING_NS`, `TASKROUTER_NS` | seed, maintain | Optional namespace mirrors. Under the `router` CLI, `ROUTING_NS` defaults to `<data home>/ns/routing` for `seed` (set it explicitly to export into a real DuckBrain namespace). Direct `router_seed.py` runs with `ROUTING_NS` unset are mirror-safe (TR-048): they resolve to the data-home scratch ns (or skip the ns export on a fresh clone); the fleet mirror needs `ROUTING_ALLOW_FLEET_MIRROR=1`. |
| `ROUTING_DOCS_DIR` | maintain | Output directory for chain snapshots. |
| `MODELSDEV_CACHE` | modelsdev, probefix | Cache path for models.dev input. |

Credentials are never configuration for this repository's committed data.
Supply them through your environment or a secret manager only. See
[SECURITY.md](SECURITY.md).

## Metrics

Every resolve appends one metrics row per chain hop: provider, model, pair,
chain order, effective price, outcome (`resolved`, `excluded`, `error`),
exclusion reason, and the configuration snapshot in force. Query it:

```bash
router metrics --top-providers 10 --since 7d
router metrics --top-models 10 --profile P1_CODING --json
router metrics --top-pairs 20 --since 24h
```

Metrics live at `$TASK_ROUTER_HOME/metrics.jsonl` under the CLI, or
`data/metrics.jsonl` repo-relative. Treat real metrics as operational data.

**Retention policy.** The ledger is bounded, not unbounded: a 30-day window with
a 2 GiB ceiling, reclaimed by an atomic window prune — never by renaming the live
file, because the writer appends to one path and the reader reads that same path.
Measured growth is ~64 MB/day (2.97 M rows / 1.48 GB over 23 days to 2026-09-23).
The full rule, the measurement table and the follow-up compaction row are in
[docs/decisions/review-tr-003-metrics-ledger-retention.md](docs/decisions/review-tr-003-metrics-ledger-retention.md).

## Data and namespaces

`data/tables/` holds the committed JSONL catalog and generated routing layer:

- Core catalog: `providers`, `models`, `benchmarks`, `archetypes`, `projects`.
- Capability derivation: `level_defs`, `model_perf`, `category_levels`, `model_tier`.
- Profile layer: `task_profiles` (versioned + tagged), `task_profile_requirements`.
- Provider operations: `provider_rules`, `probe_providers`, `probe_fixes`, `probe_excludes`, `probe_gaps`, `fallback_lanes`, `plan_terms`.
- Catalog enrichment: `provider_mappings`, `model_aliases`, `model_notes`, `model_catalog`, `temporary_discounts`, `quality_estimates`.

The JSONL tables are generated — change them through the data/seed workflow,
never by hand:

```bash
python3 scripts/router_seed.py
router spawn my-project --format json
```

**Direct seed invocation is mirror-safe (TR-048):** with `ROUTING_NS` unset,
`python3 scripts/router_seed.py` never writes the fleet DuckBrain mirror —
the ns export goes to `<data home>/scratch/ns/routing` when a DuckBrain ns
exists, and to `<duckbrain home>/namespaces/routing` on a fresh clone
(absent → the export step is skipped with a visible `ns mirror absent`
line). Writing the live mirror directly is an explicit opt-in:
`ROUTING_ALLOW_FLEET_MIRROR=1` (path override: `ROUTING_FLEET_MIRROR`), or
just set `ROUTING_NS`. `router seed` (CLI) and `router_maintain.py` always
set `ROUTING_NS` themselves, so sanctioned workflows are unaffected.

Design notes: [scheduler integration](docs/integration.md),
[registry maintenance](docs/registry-maintenance.md),
[data quality](docs/category-data-quality.md),
[soft-gate caller contract](docs/soft-gate-integration.md),
[chain snapshots](docs/chains-2026-09-01.md).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, data discipline, tests, and
commit expectations. See [SECURITY.md](SECURITY.md) for private vulnerability
reporting and the key-handling policy.

## License

MIT. See [LICENSE](LICENSE).
