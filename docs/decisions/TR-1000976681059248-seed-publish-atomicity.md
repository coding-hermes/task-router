# TR-1000976681059248 — a refused seed must publish nothing (seed publish lifecycle)

Date: 2026-11-06 · Worktree: `fix/tr-seed-refusal-atomic` (task-router) · Method: live execution of the
real seed on a worktree at 8524321, before and after the change.

Spec note: this repository has no `docs/handbook/` (no `OUTLINE.md`, no chapter IDs, no `F1`-`F16` flow
ids). The design authority for the seed is therefore this decision record plus the TR-383/TR-232
records it cites. Flow described here: **seed publish lifecycle** — build → grade → publish.

## The defect

`scripts/router_seed.py` ended its module body by publishing, and ran the TR-383 satisfiability gate
only afterwards, inside the `if __name__ == '__main__':` block:

| step | pre-fix line | effect |
|---|---|---|
| `_dump_registry()` | 1946 | wrote `registry.json` (6.0 MB) |
| ns mirror export | ~1918-1945 | wrote `$NS/tables/*.jsonl` |
| data/tables sync | 1950-1957 | rewrote the registry's 12 tracked `data/tables/*.jsonl` tables |
| **satisfiability gate** | 1960-2039 | could `sys.exit(1)` — *after* all three |

Measured consequence at 8524321 (`~/.hermes/venvs/board/bin/python3 -m scripts.router_seed`):

- exit code **1**, output ending `Seeding REFUSED: add a routable lane for the categories above …`
  and `SATISFIABILITY FAIL: 6 of 264 rating(s) … math: levels 0..5 — no lane MEASURES this capability`.
- immediately before that, the same run printed `wrote …/registry.json (5946.2 KB)` and
  `synced data/tables -> …/data/tables`.
- two consecutive refused runs changed `registry.json`'s sha256
  (`03ec51a3a68a24f7…` → `cf58762f5078aefe…`) and bumped the mtime of every table the sync
  rewrites — 12 files, the exact write set the pre-fix audit in
  `tests/test_seed_refusal_atomicity.py` records (the other tracked `data/tables` entries are
  inputs/sidecars the sync never opens).

So `make seed && make validate` (README) failed at `make seed`, while `make validate` went green on the
artifact the refusal had just written and claimed not to have seeded. The refusal was **diagnostic
only** — it never protected the served registry.

`registry.json` itself is gitignored (`.gitignore:23`), which is why the pre-fix damage did not show up
as a dirty tree: the mutation hit the live store the router serves, not a tracked file.

## The change

Publishing is now atomic with respect to the gate:

1. `_build_registry_doc()` builds the document in memory (the read half of the old `_dump_registry`,
   minus the write).
2. `_stage_run_outputs()` serializes that document into a run-unique
   `tempfile.mkdtemp(prefix='router-seed-stage-')` directory **outside the repo tree** — `registry.json`
   (`indent=1`) and one `tables/<name>.jsonl` per table (`ensure_ascii=False`, one record per line),
   byte-identical to what the old writers produced.
3. The TR-383 gate grades those **staged** tables (`load_tables(table_dir=<staging>/tables)`), keeping
   its documented intent — grade the registry the fleet is about to use, not a stale copy — its exit
   codes (1 refused, 2 unreadable tables) and every printed line.
4. `_publish_run()` runs only after a pass, or after the loud `--allow-unsatisfiable` /
   `ROUTING_ALLOW_UNSATISFIABLE=1` opt-out: ns mirror, then `registry.json`, then the `data/tables`
   sync, printing the same three markers in the same order as before.
5. The staging directory is removed in a `finally`, so a refused run leaves no artifact anywhere.
   Import-time (non-`__main__`) publishing semantics are preserved exactly as they were.

## Verification (all run in `fix/tr-seed-refusal-atomic`)

| arm | command | result |
|---|---|---|
| refusal | `python3 -m scripts.router_seed` | `rc=1`, `Seeding REFUSED`; `registry.json` sha **OK** (unchanged), 31/31 `data/tables/*.jsonl` hashes **OK**, no publish marker printed, no porcelain drift |
| opt-in | `python3 -m scripts.router_seed --allow-unsatisfiable` | `rc=0`, `seeding anyway`, published — `registry.json` sha **changed** (the gate moved, it was not disabled) |
| regression | `pytest -q tests/test_seed_refusal_atomicity.py tests/test_symlink_path_resolution.py` | 23 passed |
| full suite | `pytest -q tests/` | see the tick's board summary for the exact count |

`tests/test_seed_refusal_atomicity.py` runs the real script as a subprocess in three legs (refusal,
opt-in control, and an in-child `sys.addaudithook` open-recorder that fails if a publish write happens
before the gate). The opt-in control leg exists so the ordering guard cannot pass vacuously.

## Known, deliberately unchanged

The pre-gate TR-232 "undamp" pass rewrites `data/tables/benchmarks.jsonl` (and its `data/state` backup
twin) on every run, refused or not. It is a data-integrity normalisation, not a publish, and on the
committed tree it is content-identical. The atomicity contract is therefore stated on **content hashes,
not mtimes** — documented in the test at `test_refused_seed_publishes_nothing`.
