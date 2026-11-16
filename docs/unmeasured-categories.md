# Declared-unmeasured categories

A **declared-unmeasured** range is a part of the capability scale the registry
deliberately does not publish, because no routable lane can hold it and no
measurement exists to change that. It is a decision with a reason, recorded as
data — not a hand-edit, not a deleted ladder, not a permanent
`--allow-unsatisfiable`.

## Why this exists

The seed refuses to publish a registry in which a rating the classifier can emit
has no routable lane (TR-383). That gate is right: a rating nobody can serve is a
ticking 503. But a gate with only two outcomes — pass, or refuse forever — turns
one honest gap into a board-wide deadlock, and the pressure then goes into
whichever escape is nearest: deleting the frozen ladder (which only hands the
category to the quantile fallback, rebuilding the same levels from the same
population), hand-editing `category_levels.jsonl` (which the next seed silently
reverts), or leaving `--allow-unsatisfiable` on (a gate that only ever warns).

The measured case (2026-11-15, `t_math_unservable`): `math` is a frozen absolute
ladder whose lowest band is `-5` at `min_perf 0.99208`, authored against a
population that still contained `ollama-cloud/qwen3.5:397b` (AIME26 1.0). That
lane was retired 2026-09-25 (disabled, unpriced). The live routable math
population is now a **single value** — `glm-5.2` at `0.992`, 8e-05 below the
lowest band — so no routable lane can hold `math >= 0`, and the bands cannot be
re-derived by quantile from a one-value population. Six of 924 ratings were
unservable, and every evidence card that had to seed to propagate its rows was
blocked behind them.

## The input

`data/tables/unmeasured_categories.jsonl` — one row per range:

```json
{"category": "math", "min_level": 0, "max_level": 5,
 "reason": "why no routable lane can hold this, and what would change it",
 "declared_at": "2026-11-15", "declared_by": "t_math_unservable",
 "source": "where the decision came from",
 "recheck": "the signal that should withdraw this declaration"}
```

- `reason` is **required**. A row without one refuses the seed: a withdrawal
  nobody can explain is exactly the silent gap this input exists to prevent.
- `max_level: null` means "open-ended" (through the top of the scale).
- Levels at or below the resolver's blank default (`-1`) are **not** worth
  declaring: every lane serves them by having no tier row at all, so they are not
  a gap. Declare only the levels above the blank default.

## What the seed does with it

`scripts/router_seed.py` section 5b, after `category_levels` is built and
**before** `model_tier`:

1. validates every row (category present, reason present) — a bad row refuses the
   run before anything is published;
2. deletes the declared `(category, level)` rows from `category_levels`, printing
   `unmeasured: <cat> levels a..b WITHDRAWN from the emit domain (N level row(s))
   — <reason>`;
3. prints `declared but withdrew 0 level row(s)` when a declaration is already
   applied or stale — never silent either way;
4. carries the declaration into `registry.json` as a sidecar table, so the
   published registry and the gate grade the same bytes.

Because the withdrawal happens before `model_tier` is built, a lane whose perf
reaches into a withdrawn range keeps only its highest **published** tier. That is
the intended consequence: a tier in a range the registry does not publish is not
a tier anyone can ask for.

## Where it is visible

| surface | what it says |
| --- | --- |
| `scripts/router_seed.py` | `unmeasured:` withdrawal lines + `DECLARED UNMEASURED:` on every run, pass or fail |
| `scripts/router_satisfiability.py` | `DECLARED UNMEASURED:` lines, `declared_unmeasured` / `declared_gaps` in `--json`; a declared level is not a FAIL, an undeclared one still is |
| `/health` | `unmeasured_categories: {count, declared[], source}` (fail-open; a torn line is skipped, not fatal) |
| `data/tables/category_levels.jsonl` | the published emit domain — the declared levels are simply absent |

`DECLARED-LEVEL RESIDUE` means a domain on disk still carries a declared level
(a stale table, or a hand-built corpus): re-run the seed to withdraw it. It is
reported, never fatal.

## Withdrawing a declaration

Delete the row and re-run the seed. If the levels are still unservable the seed
refuses — which is the point: the gap comes back as a defect the moment the
reason stops being true. The `recheck` field names the signal to watch (for
`math`: a routable lane measuring `>= 0.996`).

## What this is not

It is not a way to make a gap disappear. The declaration is printed on every seed
run and every `/health` read, it names the reason and the recheck signal, and it
covers only the levels it names — a category that is merely *under-measured*
still fails the gate. The honest alternatives remain: measure a lane, or re-pin
the bands with an owner ruling.
