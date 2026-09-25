# Task-Router Dogfood Integration — 2026-09-25 (7th run)

**Angle:** the two README-flagged surfaces no prior run (09-12, 09-16, 09-20)
touched: **versioned/tagged profiles** (§ "Versioned, tagged profiles") and
**provider-mapping rename rules** (TR-019). Environment: fresh venv, isolated
data home (`TASK_ROUTER_HOME=/tmp/...`), scratch `ROUTING_NS`,
`ROUTING_DATA_DIR` scratch copy of data/tables — zero writes to live state.

## Promise under test

"Profiles are versioned like container images: the row identity is (id,
version) and the human handle is tag. Profile references resolve tag first,
then exact id... Moving a tag to a newer version is a data edit — callers
following the tag pick up the new version without code changes, while
**pinned old versions still resolve by version**. Retagging is idempotent."

And (router_seed.py docstring, TR-019): provider_mappings rules mean a
renamed external lane "keeps resolving to its canonical registry provider".

## What a real user does: version a profile

The documented flow is a pure data edit: append a v2 row to
`data/tables/task_profiles.jsonl` (new id `P3_DOCS_V2`, `version: 2`, same
`tag: P3_DOCS`), append stricter requirement rows keyed to the new
`task_id`, re-run `router seed`. That worked exactly as documented —
seed picked up both versions, no code change, `--list-profiles` and the web
UI surface both. 7.8s warm seed, no failures. This half of the promise holds.

## What breaks: the old version becomes unreachable

With `P3_DOCS` (v1) and `P3_DOCS_V2` (v2, tag `P3_DOCS`) both in the
registry, every reference form resolves to v2 or errors:

| caller writes          | resolves to | note |
|------------------------|-------------|------|
| `--profile P3_DOCS`    | v2          | tag first (as documented) |
| `--profile P3_DOCS_V2` | v2          | |
| `--profile P3_DOCS:1`  | PROFILE_NOT_FOUND (fail-open exit 0) | no pin syntax exists |
| `--profile P3_DOCS@1`  | PROFILE_NOT_FOUND | |
| `spawn P3_DOCS`        | v2          | TR-059 positional path shares `_resolve_profile_tag` |

The README's "pinned old versions still resolve by version" is a false
promise: there is no `id:version` / `id@version` reference syntax anywhere in
the resolver (`_resolve_profile_tag`, scripts/router_spawn.py:856), and the
exact id of the old row — the only pin a caller could write today — is
shadowed by the tag match, because tag matching runs BEFORE the exact-id
fallback on the same string. The one-sentence docs fix is to tell callers the
only supported "pin" is to reference the old version's distinct id while its
tag is untouched... which the tag-first rule defeats the moment the tag
moves. Resolution semantics for `p3_docs` (lowercase) also error with no
near-miss hint (the hint machinery only fires from the PROJECT slot, not
`--profile`).

Root cause chain for the foreman:
- `task_profile_requirements` PK is `(task_id, category)` — no version
  dimension — so v1/v2 requirements live under different task_ids and the
  "version" of a profile is really "a different profile that shares a tag".
- `profiles` dict in resolve() is keyed by id; the version-desc sort in
  `_resolve_profile_tag` (scripts/router_spawn.py:863) only orders rows that
  share the TAG — an exact id that is also a tag of a newer row loses.

## What a real user does: rename a provider lane via mapping rules

Appended a `models.jsonl` lane under provider `eu-openai` (copy of an
existing synthetic row) plus a mapping rule `{pattern: eu-openai, match:
literal, replacement: <canonical>, direction: external->registry}`. Seed
prints the reconciliation line as advertised:
`mapping: external lane 'eu-openai' -> canonical provider '<canonical>'`.

Then the real question: does the renamed lane ROUTE? **No.** The lane is
seeded into the registry under its external id `eu-openai` (the mapping is
never applied to the models row), so it appears in zero chains —
`router spawn --profile-req ...` chains contain the canonical provider's own
lanes but never the renamed lane. The docstring's claim "keeps resolving to
its canonical registry provider" holds only for the seed's REPORT, not for
routing. `router modelsdev sync --dry-run` treats the same external name as
UNMAPPED (the rules apply there only to models.dev catalog ids). Two
consumers, two behaviors, and neither routes the lane the docs describe.

## Measured (Step 2b)

- headline op `router spawn --profile P6_DEFAULT --format json --quiet`,
  2.7MB registry, 1538 live lanes: **76.3 ms ± 25.2 ms warm (20 runs,
  hyperfine), 91 ms cold (drop_caches)**. Nothing here is slow enough that a
  user would notice — no PERF row filed, and that is the honest result.
- seed: 7.8s warm on this box; 11s on the fresh bunker (Python 3.13.5).
- install leg: clone 5.5s → venv+`pip install -e .`+duckdb 18s → seed 11s →
  resolve/estimate/health all green on a bare Debian 13 box (agent f7dc60b9,
  destroyed after).

## Verdict

SHIPPABLE (unchanged from 09-12/09-16) — the core resolve/estimate/gate
workflow remains solid and fast, the install path is clean on a fresh box.
But the two flagship README claims probed this run do not hold in real use:
version pinning does not exist, and mapping rules do not route renamed
lanes. Both are doc-vs-reality defects with cheap honest fixes (see board
rows TR-131/TR-132).
