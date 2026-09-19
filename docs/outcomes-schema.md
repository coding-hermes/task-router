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
| `ts` | number | **yes** | epoch seconds; the decay anchor. Defaults to the ingest time when absent. |

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

Bucket = `(source_system, provider, model, complexity)` — i.e. one row per
backend **and** one row per `(model × complexity reference)`, which is what
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

## Seed derivation

`scripts/router_seed.py` emits the averages as the registry table
`model_outcomes` (text sidecar, exactly like `fallback_lanes`) and exports it to
the routing namespace `tables/model_outcomes.jsonl`. It is deliberately **not**
written into the committed `data/tables/` mirror: it is per-user data derived
from the gitignored store, so a clone without outcomes simply gets an empty
table.

| column | source |
|---|---|
| `source_system`, `provider`, `model`, `complexity` | bucket key |
| `avg_cost_task_<N>h`, `avg_wall_time_<N>h`, `avg_turns_<N>h` | decay-weighted means |
| `n_samples`, `n_completed`, `n_success_known`, `success_rate` | counts |
| `backends` | comma-joined backend list (merged rows) |
