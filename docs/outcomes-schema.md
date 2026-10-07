# Outcomes store — cost-per-task engine (TR-049)

Routing used to rank lanes by **cost per token**. The unit that actually
matters is **cost per completed task**: a lane that is cheap per token but
needs three attempts is not cheap. This document is the schema contract for the
store that makes that measurable.

Three files, three roles:

| file | role | committed? |
|---|---|---|
| `data/state/outcomes.jsonl` | append-only outcome store (one row = one finished task/session) | **no** (gitignored — per-user data) |
| `data/state/outcomes-averages.jsonl` | rolling (exponential-decay) averages derived from the store | **no** (gitignored, derived) |
| `data/tables/sample-outcomes.jsonl` | small synthetic sample for docs/tests | yes |

Doctrine: the store is **your** data. It is never published with the repo;
each user rebuilds the averages from their own gateway/agent DB (`scripts/drivers/`).
The committed sample exists so a fresh clone can read a row shape, nothing more.

## Paths

| what | override env var | default |
|---|---|---|
| outcome store | `ROUTING_OUTCOMES_FILE` | `<repo>/data/state/outcomes.jsonl` |
| rolling averages | `ROUTING_AVERAGES_FILE` | `<repo>/data/state/outcomes-averages.jsonl` |

Both are resolved **at call time** (`router_outcomes.outcomes_path()` /
`averages_path()`), so an env change reaches the API server, the averages CLI
and the seed without a code change. The repo-level default is deliberate: the
store is runtime state, like `data/metrics.jsonl`, and the writers/readers must
agree on one path (the `router` CLI wrapper does not re-point it, for the same
reason it does not re-point `metrics`).

## Row schema

One JSON object per line. Field order below is the order the ingest writes;
consumers must read by key, never by position.

| field | type | required | notes |
|---|---|---|---|
| `source_system` | string | **yes** | the backend that produced the task (`hermes`, `opencode`, …). The isolation dimension for per-backend stats. |
| `session_id` | string | **yes** | the backend's own session/trace id. Together with the model it is the dedupe key. |
| `task_label` | string \| null | no | free-text label (`task` column of the source DB). |
| `complexity` | object \| string \| null | no | **the complexity reference**: per-category required levels `{"code_gen": 2, "guard": 0}` (a profile signature), or a profile id string, or `null` when the producer did not declare one. Never invented. |
| `profile_id` | string \| null | no | routing profile the task ran under (`P1_CODING`, …) when known. |
| `required_categories` | object \| list \| null | no | raw per-category requirements when the producer has them. |
| `provider` | string | **yes** | router provider id (derived from the immutable `billing_base_url` host on the Hermes driver — see below). |
| `model` | string | **yes** | model id as the provider reports it. |
| `turns` | integer \| null | no | API calls / agent turns the task took. Sort key `turns`. |
| `tokens_in` / `tokens_out` / `tokens_reasoning` | integer \| null | no | token accounting (input includes cached reads on the Hermes driver). |
| `cost_usd` | number \| null | no | task cost in USD. Wire alias: `cost`. `null` = unknown, never `0` as a stand-in. Values above `$1000` for a single task are rejected by the Hermes driver as corrupted. |
| `wall_time_s` | number \| null | no | wall-clock seconds. Wire alias: `wall_time`. Sort key `wall_time`. |
| `success` | boolean \| null | no | did the task complete? `null` = the source does not report completion (the Hermes gateway does not) — never inferred. |
| `caller_session_key` | string \| null | no | **the caller's own join key (TR-173)**: the `X-Hermes-Session-Key` the caller sent, when it sent one — for the scheduler this is the TICK the call served. Validated with the same rules the forwarded header answers to (no control characters, ≤256 chars); a value the gateway itself would reject is never persisted, and the drop is named in the response envelope's `_router.problems`. `null` = the caller sent no key (or one that failed validation) — never a synthesized placeholder, never `""` standing in for "unknown". Together with `session_id`, `parent_session_id`, `provider`, `model` and `cost_usd`, this single ledger line answers "what did tick X route to, and what did it cost" without guessing. |
| `ts` | number | **yes** | epoch seconds; the decay anchor. Defaults to the ingest time when absent. |

### Joining a routed call to the task that asked for it (TR-173)

A caller that speaks the gateway's session protocol (the scheduler does) sends
its own id as `X-Hermes-Session-Key` — for the scheduler, the TICK id. The
proxy stores that value verbatim (after validation) in `caller_session_key`,
next to the router's own identities:

| field | whose id it is |
|---|---|
| `caller_session_key` | the CALLER's key from the request headers — the per-TASK join key |
| `session_id` | the ROUTER's row identity (`x-router-session` derived, or generated) |
| `gateway_session_id` | the UPSTREAM Hermes session id from the response headers |

So "what did tick `tg:…:2` route to, and what did it cost" is one grep over
the store — no guessing, no cross-file join. The web UI flow view
(`scripts/router_ui_data.flow`) exposes the same field in its outcome block,
and its search matches on it, so an operator can paste a tick key straight
into the trace box.

## Ingest API

`POST /api/v1/outcomes` on the router server (`scripts/router_server.py`,
started with `router server`).

* **Auth behaves exactly like every other mutation**: the server must run in
  `--mode edit` with `ROUTER_EDIT_API_KEY` set, and the caller sends that value
  in `X-API-Key`. Read-only mode answers `403`; edit mode without the key
  answers `401`. Reporters that cannot hold a key should write the JSONL
  directly (that is what `scripts/drivers/hermes.py` does) — the endpoint is for
  remote reporters.
* **Validation** is all-or-nothing: a malformed payload gets `400` naming
  *every* problem (never a partial accept).
* **Fail-open on the write**: a store problem (disk full, permissions) answers
  `200` with `{"appended": false, "error": "write failed: …"}` so a reporter is
  never blocked by our disk — it retries on its next tick.
* **Dedupe**: a re-POST of the same `(source_system, session_id, model)` found
  in the recent tail (`tail_lines`, default 2000 rows) is reported as
  `{"appended": false, "reason": "duplicate …"}` and not written. The guard is a
  bounded tail read so live ingest stays O(tail) on a ~90 MB store; older
  duplicates are accepted (the average is decay-weighted, so a stale duplicate
  cannot dominate a bucket).
* Writes are `flock`-serialized and `fsync`ed, so the API server and the batch
  importer can append concurrently.

```bash
curl -sS -X POST http://127.0.0.1:9092/api/v1/outcomes \
  -H 'X-API-Key: '"$ROUTER_EDIT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"source_system":"opencode","session_id":"sess-42","task_label":"GAP-076",
       "complexity":{"code_gen":2,"guard":0},"provider":"deepseek",
       "model":"deepseek-v4-flash","turns":11,"tokens_in":52000,"tokens_out":8100,
       "tokens_reasoning":640,"cost":0.0131,"wall_time":412.5,"success":true}'
```

```json
{"appended": true, "reason": "appended",
 "store": "/home/you/task-router/data/state/outcomes.jsonl",
 "row": {"source_system": "opencode", "session_id": "sess-42", "...": "..."}}
```

The same operation is exposed to MCP as the tool `ingestOutcome` (the MCP tool
list is derived mechanically from the OpenAPI paths, so no extra wiring).

## Bulk import (drivers)

```bash
python3 scripts/router_outcomes.py import-hermes          # the Hermes driver
python3 scripts/router_outcomes.py query --provider deepseek   # inspect averages
```

Bulk import is idempotent over the **whole** store (it scans once, then appends
only unseen `(source, session, model)` keys) — the opposite trade-off from the
HTTP path, which guards a bounded tail. See `docs/drivers.md` for the plugin
contract.

Hermes driver specifics worth knowing before you trust a row: `billing_provider`
and `model` columns in `state.db` are **re-stamped to the gateway's config
defaults at every restart**, so the driver derives the provider from the
immutable `billing_base_url` host and treats column-drift-era rows (purely
numeric provider/model) as non-lanes rather than lanes.

## Rolling averages

`scripts/outcomes_averages.py` reads the store and writes the averages table
(one JSON object per bucket per line):

```bash
python3 scripts/outcomes_averages.py --dry-run               # print, write nothing
python3 scripts/outcomes_averages.py --windows 1d,3d,7d,30d  # configurable windows
python3 scripts/outcomes_averages.py --merge-backends        # collapse source_system
```

A window is a **half-life**, like the Linux load average: a sample exactly one
window old contributes weight `0.5`; two windows old, `0.25`. Defaults are
1d/3d/7d; `--windows` accepts hours (`48`) or durations (`2d`, `12h`).

Bucket = `(source_system, provider, model, complexity_sig)` — i.e. one row per
backend **and** one row per `(model × complexity SET)`. `complexity_sig` is
`sha1(canonical_json({category: min_level}))` (TR-065 R1): dict-order
independent, level-sensitive, so `{code_gen:2, test:1}` and `{security:2,
review:0}` on ONE model are two independent buckets. Rows that declare nothing
share the `null` signature (the per-model average for callers who never declare).
`required_categories` is carried alongside for readability. Which is what
makes a lane's cost comparable at the task profile it will actually be asked to
serve. `--merge-backends` collapses `source_system` (sample-count weighted, so a
100k-sample backend is not averaged as an equal of a 2-sample one) and adds a
`backends` list to each row.

Per window the bucket carries every sort-key input, each `null` when no sample
carries it:

| field | meaning |
|---|---|
| `avg_cost_task_<N>h` | decay-weighted mean `cost_usd` — **cost per completed task** |
| `avg_wall_time_<N>h` | decay-weighted mean `wall_time_s` |
| `avg_turns_<N>h` | decay-weighted mean `turns` |
| `avg_tokens_in_<N>h` | decay-weighted mean input tokens per task |
| `avg_tokens_out_<N>h` | decay-weighted mean output tokens per task |
| `avg_tokens_total_<N>h` | decay-weighted mean (in + out) per task |

**Sort rules** (TR-065 R6): every metric above is sortable by name
(`predicted_cost_per_task`, `wall_time`, `turns`, `tokens_in`, `tokens_out`,
`tokens_total`) and usable in compound mixes — `--sort ratio:0.7*cost+0.3*turns`,
`--sort ratio:1*tokens_total`. The metric registry in `router_spawn.py`
(`METRIC_FIELDS`) plus the `avg_*` keys computed here are the only two places
that need a line for a NEW metric to become sortable; unknown metrics degrade
visibly to price, never silently.
| `n_samples` | rows in the bucket |
| `n_completed` / `n_success_known` / `success_rate` | completion counts; `success_rate` is `null` when the source never reports success (never fabricated) |

The file is rewritten atomically (`*.tmp` + rename); a missing or empty store
yields a JSON summary with `buckets: 0` and exit `0` (fail-open).

## Resolve-time use

`scripts/router_spawn.py` consumes the averages table:

* `--sort <key>` — `predicted_cost_per_task` (the documented default key),
  `wall_time`, `turns`, or a mix `ratio:<w1>*<cost>+<w2>*<time>` (both terms are
  min-max normalized across the eligible lanes before blending, and a lane with
  no sample for a term is treated as unknown = worst rather than as best).
  `price` reproduces the historical ordering; the CLI default stays `price` so a
  shared/symlinked binary never silently re-ranks the live fleet — pass the flag
  (or flip the default deliberately) to opt in.
* `--backend <name>` — isolate the stats lookup to one `source_system`;
  `--merge-backends` aggregates across all (default when no `--backend` given).
* The same two knobs are accepted by the HTTP `GET /resolve` endpoint as
  `?sort=…&backend=…&merge_backends=1`.

Predicted cost per task for a lane = its decay-weighted average cost when the
store has a sample for it, otherwise the price proxy
(`normalized_price × token_factor`) — an unknown lane keeps its price rank
instead of being treated as free.

## Verified outcomes (TR-299) — the actual served lane + independent pass/fail

`scripts/verified_outcomes.py` is the steady-state consumer of every completed
task outcome. It projects (gateway `state.db` × board `tasks.jsonl`) into a
second store — `data/state/outcomes-verified.jsonl` (path env:
`ROUTING_OUTCOMES_VERIFIED_FILE`) — whose rows the rolling averages fold in:

```bash
python3 scripts/verified_outcomes.py --dry-run     # collect, write nothing
python3 scripts/verified_outcomes.py               # atomic projection write
python3 scripts/outcomes_averages.py --extra-input data/state/outcomes-verified.jsonl
```

What a verified row adds over a plain Hermes-driver row:

| field | meaning |
|---|---|
| `provider`, `model` | the lane that ACTUALLY served the session — derived from the immutable `billing_base_url` host, never the re-stamped `billing_provider` label |
| `tokens_in` (incl. cache reads), `cache_read_tokens`, `tokens_out`, `tokens_reasoning`, `turns` | the session's exact meter, main lane only |
| `cost_usd` + `price_basis` | the gateway estimate, with the plan-effective replacement when the meter reads zero (TR-070) and NULL + reason above the $1000 corruption bound |
| `success`, `acceptance_status`, `acceptance_source` | pass/fail from the board row's acceptance evidence (closure, `worker_status`, per-criterion results, evidence) — worker prose in title/detail is deliberately NOT a verdict; `null` always carries `unranked_reason` |
| `complexity_sig` / `band` | the board row's `required_categories` through the SAME `band_key()` the resolve side uses (R3.1: one function, write side and read side) |
| `task_key` | the board join key (session display_name/session_key; timestamped foreman run keys collapse to their stable project prefix) |

Derivation rules worth trusting:

* A session's side-purpose calls (`title_generation`, `approval`,
  `background_review`, `compression`, `vision`, `goal_judge` — measured
  2026-10-06) are NOT the task's lane and are excluded by name. A non-empty
  `task` value outside that set is billed to NOTHING and the row says so
  (unclassified, with the reason) — never silently folded into the main lane.
* The write is an ATOMIC PROJECTION (tmp + rename), not an append: the same
  (DB, board) state yields a byte-identical store, so re-running a session
  adds no row and never double-counts (the accumulate path is for live steps
  landing one at a time; a full re-derivation must replace, not add).
* `--extra-input` folds the verified store into the rolling averages, and a
  verified row SUPERSEDES a main-store row with the same identity key
  (`source_system, session_id, model`) — the billed-lane projection and the
  actual-lane projection describe one session; counting both bills it twice.

Rolling-average fields the verified leg adds per bucket:

| field | meaning |
|---|---|
| `cost_per_passed_task_<N>h` | decay-weighted cost over PASSED tasks only (independent verdicts). NULL with `cost_per_passed_task_basis` when the bucket holds zero passes (`no-passed-samples: 0 of N …`) or no verdicts (`unverified: N of M …`) — a cheap lane that fails its tasks cannot read as cheap-per-passed-task |
| `n_passed` | rows whose independent verdict is True |
| `ranking` gate | `router_outcomes.ranking_verdict(entry)` → `(ranked, reason)`: below `ROUTER_SORT_MIN_SAMPLES` (the same knob as the resolve-side measured floor, default 3) independently-verified samples a bucket stays unranked WITH a reason naming the counts |

Failure mode is fail-open end to end: a missing gateway DB, an unreadable
board, or a task absent from the board produce reasoned rows (or an empty
result), never a crash — nothing here may block the resolve path.

## Seed derivation

`scripts/router_seed.py` emits the averages as the registry table
`model_outcomes` (text sidecar, exactly like `fallback_lanes`) and exports it to
the routing namespace `tables/model_outcomes.jsonl`. It is deliberately **not**
written into the committed `data/tables/` mirror: it is per-user data derived
from the gitignored store, so a clone without outcomes simply gets an empty
table.

| column | source |
|---|---|
| `source_system`, `provider`, `model`, `complexity_sig` | bucket key |
| `required_categories` | the declared requirement set behind the signature |
| `avg_*_<N>h` (cost, wall_time, turns, tokens_in/out/total) | decay-weighted means |
| `n_samples`, `n_completed`, `n_success_known`, `success_rate` | counts |
| `backends` | comma-joined backend list (merged rows) |
