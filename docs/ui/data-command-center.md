# Data Command Center — UI data plane routes

The task-router server ships a small UI and the JSON APIs behind it, served by
the same process that resolves tasks (`scripts/router_server.py --mode
read-only`, default port 9092). The route table in the source (TR-150..TR-156
descriptions, ~lines 106–146) is the contract; this page documents it in prose.

- `GET /ui` serves the whole page as one self-contained document — no CDN, no
  build step, read-only.
- Seven `GET /api/ui/*` endpoints feed its panels.
- One mutation, `POST /api/ui/registry/edit`, exists for edit mode.

Every search/scan endpoint follows one honesty rule (TR-151): **a response
always says how much of the store it actually read** — `rows_scanned`,
`scan_limit`, `scan_truncated` — and `truncated` is true whenever the scan
window or the page cut the result short, so a search can never look complete
when it was not.

Screenshots (audited 2026-09-26): see `docs/ui/data-command-center-20260926.png`
and the per-panel shots
(`...-hero.png`, `...-registry.png`, `...-chain.png`, `...-flow.png`,
`...-series.png`, `...-board.png`), plus the verified set in
`docs/ui/audited-20260926/`.

---

## GET /ui

The Data Command Center page itself (TR-150). One HTML document served by this
service — self-contained, no external assets, read-only. No query params.

## GET /api/ui/registry

Browse the registry (TR-155, read half): a lane detail **joined** across the
generated tables — `models.jsonl` (prices, lifecycle), `model_tier.jsonl`
(per-category levels), `quality_estimates.jsonl`, `providers.jsonl` (plan /
data class) — and each part of the response names the file it came from
(`level_source_file`, `estimate_source_file`, `provider_source_file`), so a
number on screen traces back to a table.

Query params:

| param | notes |
|---|---|
| `q` | free text over provider/model and price evidence |
| `provider` | exact provider id |
| `category` | filter to lanes with a level in this category; an unknown category is REFUSED with an error listing the known ladder, not ignored |
| `min_level` | with `category`: minimum tier |
| `plan_tier` | exact match on the lane's plan tier |
| `lifecycle` | `dated` / `undated` / `retired` (dated = has valid_from or valid_to; retired = has valid_to) |
| `limit` | page size, default 25, max 200 |

Honesty fields: `rows_scanned` (number of `models` rows read), `matched`,
`returned`, `truncated` (true when matches exceed the returned page),
`known_categories`, `level_ranges`.

## GET /api/ui/chain

The eligible chain in effective-price order, plus EVERY excluded lane grouped
by machine-readable exclusion code (TR-154). Answers "why this lane and not
that one". Backed by a live `router_spawn.py` resolve (90 s timeout); a failed
resolve is reported as `{"error": ..., "chain": [], "exclusions": []}` rather
than a 500.

Query params:

| param | notes |
|---|---|
| `project` | project id (default `coding-hermes-scheduler`) |
| `profile` | profile id, e.g. `P1_CODING` |
| `sort` | ordering passthrough (default price) |
| `window_h` | stats window for stats-based sorts (passed through to the resolver) |

Honesty fields: each chain lane carries its `price_basis` (list vs the
plan-effective figure it is actually ordered on, or `no declared price for
this lane`). Exclusions appear three ways so nothing is silently cut:
`excluded_total` / `excluded_shown` / `exclusions_truncated` (detail is capped
at 60 rows), `exclusion_summary` (counts per code), and `exclusions` (the
shown detail). `chain_length` names the eligible count.

## GET /api/ui/flow

The whole story of ONE request, keyed by session id (TR-153): how it was rated
-> the requirements -> the chain considered -> the hops attempted -> the lane
that served it -> cost + basis, reconciled with the gateway session when that
is reachable.

Query params:

| param | notes |
|---|---|
| `id` | **required** — session_id (the router proxy's own, or the caller's); `session` is accepted as an alias |
| `scan_limit` | store rows to tail-scan (default 200000) |

Honesty fields: `artefacts_read` / `artefacts_available` /
`artefacts_missing` name which of the three artefacts (envelope, ledger row,
gateway session) it could actually read — a drill-down never silently shows
two of three. `rows_scanned` + the `note` state the scan window
("newest N of M store rows"); a not-found session says whether it may predate
the window or never reached the ledger. `skipped_explanation` distinguishes a
gate SKIP from a failed attempt; `chain.truncated` / `chain.excluded` carry
the chain-side truncation flags from the ledger row.

## GET /api/ui/series

Traffic + cost over time, bucketed hourly or daily, by lane / band / total
(TR-152).

Query params:

| param | notes |
|---|---|
| `bucket` | `hour` (default) / `day`; anything else falls back to hour |
| `window_h` | how far back to read (default 24) |
| `group` | `total` (default) / `lane` / `band` |
| `scan_limit` | store rows to tail-scan (default 200000) |

Honesty fields: every bucket carries `requests` / `served` / `failed` /
`unknown`, `tokens_in` / `tokens_out`, and its sample counts — `cost_usd` is
`null` with `cost_reason: "no priced sample in this bucket"` when nothing in
the bucket was priced (a cost column built from unpriced rows would read as
free). Buckets with no traffic are emitted as empty (`note: "no traffic in
this bucket"`), not omitted, so a quiet hour stays visible on the axis.
Response-level: `rows_in_window`, `rows_scanned`, `parse_failed`,
`scan_window` ("newest N of M store rows"), `window_start_ts` /
`window_end_ts`.

## GET /api/ui/board

Search the board JSONL (`.coding-hermes/board/tasks.jsonl`) for the UI's board
panel (TR-150/156). Same honesty contract as the ledger search plus a board
ID CENSUS and ARTIFACT CHECKING: cited `files_changed` paths are verified to
exist on disk (up to 200 checks) and a missing one is flagged, not linked as
if it were there.

Query params:

| param | notes |
|---|---|
| `q` | free text over id, title, status, priority, reasoning, foreman/worker notes, files_changed, commit_hash, capability_tags |
| `id` | id prefix match |
| `status` | exact board status |
| `priority` | exact priority |
| `commit` | substring match on commit_hash |
| `limit` | page size (default 25, max 200) |
| `offset` | matches to skip |

Honesty fields: `total_rows` / `total_matched` / `rows_scanned` / `truncated`
(like the ledger search; `scan_truncated` is always false here — the board is
read in full). `census` = `{rows, duplicate_ids, by_status, max_id}` because a
duplicated id is how two agents overwrite each other's row.
`artifacts_checked` / `artifacts_missing` + per-row `artifacts[]` for the
existence checks. `filters_available` names what is real and `filters_absent`
names what is not (`owner` — the board carries no owner field).

## GET /api/ui/ledger

Search the outcome ledger — the raw data every other stats surface summarises
(TR-151). Free text plus structured filters; the page-over-store search the
other panels' honesty fields are modelled on.

Query params:

| param | notes |
|---|---|
| `q` | free text over provider, model, session_id, task_label, failure_reason, served_by_hop, complexity_sig (band), source_system, price_basis |
| `provider` | exact provider id |
| `model` | exact model id |
| `band` | complexity_sig to match |
| `outcome` | `success` / `failed` / a route_outcome value |
| `complexity_source` | exact match |
| `since` | epoch seconds lower bound (inclusive) |
| `until` | epoch seconds upper bound (inclusive) |
| `order` | `recent` (default — newest `scan_limit` rows, served newest-first) / `oldest` (walks from the head) |
| `limit` | page size (default 50, max 500) |
| `offset` | matches to skip |
| `scan_limit` | rows to read before stopping (default 200000); reported back as `rows_scanned` |

Honesty fields: EVERY response carries `rows_scanned`, `scan_limit`,
`scan_truncated` and `total_matched`, and `truncated` is true whenever either
the scan window or the page cut the result short — so a search can never look
complete when it was cut short. With `order=recent` a browser over a 325k-row
ledger starts at the newest end instead of showing the oldest rows first.

## POST /api/ui/registry/edit

The one UI mutation (TR-155, write half). Guarded, validated, audited,
revertible. Body fields: `provider` and `model` (required — they identify the
lane), optional `public_price`, `normalized_price`, `public_in_per_m`,
`public_out_per_m`, `valid_from`, `valid_to`, `lifecycle_source`, `plan_tier`,
`context_limit`, `disabled`, `category`, `level`, `revert_of` (timestamp of
the audit entry to revert).

Auth: needs the edit key in an `X-Edit-Key` (or `X-API-Key`) header, and the
server must run in edit mode (`--mode edit` with `ROUTER_EDIT_API_KEY` set).
In read-only mode the request never reaches this handler — the auth gate
answers `403 {"error": "read-only mode"}` for every mutation.

Guarantees (all refusal paths are audited to `data/registry-edits.jsonl`):

- no key / wrong key -> 403, audited; a server with no edit key configured
  refuses with a "read-only posture" 403;
- validation refuses nonsense rather than clamping it: an unknown category, a
  level outside the ladder, a price an order of magnitude (10x / 0.1x) off the
  published figure, a non-ISO date, a lifecycle stamp without a date, missing
  provenance (`lifecycle_source`) on a lifecycle stamp -> 422 with the
  problems listed;
- unknown lane -> 404;
- the generated JSONL tables are NEVER written — the change is appended to
  the OVERLAY channel `data/lifecycle.jsonl`, which the seed path applies
  key-wise; the response states `overlay_appended: true, generated_tables_touched: false`;
- every accepted change appends an audit row carrying actor, before, after
  and a timestamp, and the response includes a ready-to-post `revert` body
  (`revert_of` makes the inverse append a first-class operation);
- `public_price` and `normalized_price` stay DISTINCT — an edit that sets one
  never rewrites the other.

This mutation has no scan/honesty fields; its honesty surface is the audit
log: refusals and accepts are both recorded with `outcome`, `reason`,
`lane`, `changed`, `before`, `after`.
