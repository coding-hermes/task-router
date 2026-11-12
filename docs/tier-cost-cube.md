# The effort-aware tier x cost cube (TR-402)

Answering "is **GLM at `high` effort** worth **xkiro at `default`** for
`code_gen`?" needs three facts joined on one key. All three are published —
committed JSONL in `data/tables/`, carried verbatim inside `registry.json` and the
DuckBrain `routing` namespace mirror — so the cube is reconstructible from a
checkout with no database, no live service and no second writer.

## Sources

| File | Key | Carries | Nature |
|---|---|---|---|
| `data/tables/models.jsonl` | `(provider, model)` | liveness (`disabled`, `archive`, `valid_to`), prices | lane |
| `data/tables/model_tier.jsonl` | `(model, category, reasoning_effort)` | `tier` (−5..+5), `tier_source`, `source_ref` | **model** |
| `data/tables/cost_effort.jsonl` | `(provider, model, reasoning_effort)` | `est_cost_per_task`, `basis`, `n_samples`, `null_reason` | **lane** |

The asymmetry is the doctrine, not an accident: **capability is a property of the
weights** (Bane 2026-08-27), so the same model sold by six providers gets six cube
rows with the *same* tier and *six different* costs.

## Reconstruct it

```bash
# coverage summary as JSON (cells, measured ratio, explained-null ratio)
python3 scripts/tier_cost_cube.py

# the cube itself, one JSON object per cell
python3 scripts/tier_cost_cube.py --rows

# one category / one effort
python3 scripts/tier_cost_cube.py --rows --category code_gen --effort high

# the data-quality gate (exit 1 on a duplicate, an unexplained NULL, an
# effort outside the vocabulary, or a tier row with no effort key)
python3 scripts/tier_cost_cube.py --check
```

In the warehouse the same join is a view, installed by `router_seed.py` from the
single `CUBE_VIEW_SQL` in `scripts/tier_cost_cube.py`:

```sql
SELECT provider, model, category, reasoning_effort, tier, tier_source,
       est_cost_per_task, cost_basis, n_samples, cost_null_reason, tier_per_dollar
FROM v_tier_cost_effort
WHERE category = 'code_gen'
ORDER BY tier DESC, est_cost_per_task ASC;
```

## Columns

| Column | Meaning |
|---|---|
| `provider`, `model`, `category`, `reasoning_effort` | the cell key |
| `tier` | signed level −5..+5 at that effort (`null` never appears: a missing tier row means the model has no evidence in that category — the resolver reads that as −1, it is not a cube cell) |
| `tier_source`, `tier_source_ref` | where the tier came from (`benchmark`, `family`, `estimate`, …) — an estimated tier is labelled, never disguised as measured |
| `est_cost_per_task` | measured median cost of the task shape at that effort; **`null`, never `0`** |
| `cost_basis` | `measured` \| `unmeasured` \| `unpriced` \| `no_cell` |
| `n_samples` | outcome rows behind a measured cell |
| `cost_null_reason` | why the cost is null — see below |
| `tier_per_dollar` | `tier / est_cost_per_task`, only when the tier is **> 0** and the cost is **> 0** |

## What a NULL means (each one carries its reason)

- `no outcome rows at effort='high' (…)` — the lane is priced, the effort is not
  measured. TR-399 landed the ingest stamp; rows accumulate per effort over time.
- `no cost_effort cell for this lane at this effort` — the cost generator never
  enumerated this (provider, model, effort). Never silently priced off a sibling.
- `cost cell is NULL and the generator supplied no null_reason (…)` — an upstream
  defect, named as one rather than rendered as a hole.
- `tier_per_dollar` is additionally null when the tier is ≤ 0 (`tier <= 0: not an
  accuracy worth a per-dollar ratio`) or when the cost is missing. A ratio built on
  a non-positive tier or a zero cost would sort straight to the top of a ranking
  and be wrong in the most expensive direction.

Zero is never used for "unknown": a `0` cost wins every price-ordered chain.

## Where each surface stops

- `/metrics` carries the **aggregate**: `task_router_tier` (live lanes per
  `category × reasoning_effort × tier`), `task_router_tier_cost_cells` (cells per
  `basis`, i.e. how much of the ladder is costed) and
  `task_router_tier_effort_levels` (`1` = only the `default` pointer exists).
  Deliberately not per-lane — see `docs/observability-spec.md` §7.
- The **per-lane cube** is data: this script, `v_tier_cost_effort`, or a plain
  `jq`/`sqlite`/`duckdb` join over the three files above.

## Current state (2026-11-12, published tables)

- 6 324 cube cells over live lanes; **1 744 measured** (27.6 %), 4 580 explained
  nulls (100 % explained).
- Every tier row is keyed `default` — the effort axis is *plumbed* (storage,
  ingest stamp, cost, resolver, cube) but **not yet populated** at ranked
  efforts. `task_router_tier_effort_levels` reports `1`; that number is the
  honest state, and TR-403 is the backfill that moves it.
