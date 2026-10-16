# Dogfood run 34 — 2026-10-16 — TR-277 data cache + TR-212 quota plane, real use

Clone: 73a7367 (HEAD c16714d at run time, main). Install leg: bunker-mvp agent
69f28085, destroyed after the run (0 residue on the 90s recheck).

## Promise under test

"A user resolves a task to a price-ordered, gate-filtered model chain"
(README quickstart), extended by the 64-commit delta since dogfood run 33:
the TR-277 runtime data cache (read once, invalidate on change, background
reload, kill switch) and the TR-212 quota remaining panel + TR-209/210/213
L2/L3 quota plane (accounting / pacing / burn / chain headroom).

## Install (fresh box, bunker-mvp, Python 3.12.3)

- clone 2s -> venv + `pip install -e .` 9s -> `pip install duckdb` + `router
  seed` 16s -> first real resolve. Total ~36s wall through `bunker exec`.
- `router validate`: 14/14 checks on the fresh clone (registry, freshness,
  lifecycle provenance, state files, profiles).
- `router spawn P1_CODING` on the fresh box: 0.25s, 11-hop chain.

## Real use, findings

### The cache under a running server (TR-277) — works

- Served 5 data tables through the cache (`/health` data_cache: hits 5,
  misses 0, inotify watchdog).
- Appended a row to `data/tables/models.jsonl` under the live server:
  reloads incremented, 0 served_stale, 0 reload_errors; the next
  `/api/ui/registry?q=<probe>` read returned the appended row
  (rows_scanned 1974). Full-file rewrite (new inode) also reloaded.
- Stat-poll fallback verified (inotify_mode none after rewrite; the box
  without watchdog still read fresh within the poll interval).
- Kill switch: `ROUTER_CACHE=0` spawn chain byte-identical to cache-on,
  rc=0 both (the d024cc1/bb71603 fixes do what they say).
- 30/30 tests green (test_data_cache + test_cache_spawn_equivalence +
  test_quota_panel, 7.9s).

### The quota plane (TR-212/209/210/213) — works on :9092, panel missing on :9093

- `/api/ui/quota` on the API server: 92 L0 windows, rung stamped per row,
  37 windows with a declared limit, 22 with a computed remaining, NULLs
  carrying `unknown (not-published)` / `no derivation for per_minute`
  verbatim, burn candidates + countdown, `generated_from` names every
  input. Honesty contract holds in real output.
- L2 accounting: synthesized 20 ledger rows ($0.30 each) + a limit config
  -> spent $6.00 / limit $70.00 / remaining $64.00, resets_at correct.
- L3 pacing: `router quota pacing --cost-c 60` refuses explicitly with the
  window named (remaining < est, margin 0.90), counters counted.
- L3 burn: verdict burn with $60.53 surplus and marginal cash NULL with
  reason (plan_kind undeclared) — never guessed to $0.
- FINDING TR-324: on the `router web` door (:9093) `/api/ui/quota` is 404
  and the embedded page has no quota panel at all, while its own comment
  claims "the command center, same page, no second UI". README's UI-data-
  plane list does not distinguish the two doors.

### Fresh-machine suite (QA-TR-FRESH-001) — re-proved on unpatched HEAD

- Scratch clone c16714d WITHOUT the working-tree fix: simulating a duckdb-
  less fresh venv reproduces the collection ERROR exactly (module-level
  `import duckdb` in scripts/router_maintain.py reached from
  tests/test_pricing.py).
- The uncommitted fix in the main checkout (guarded import + module-level
  skip) resolves it; with duckdb present, tests/test_pricing.py 21/21
  green (84.7s). Filed TR-326 + dated recurrence event for
  QA-TR-FRESH-001 — the fix needs to be landed by the owning lane.

## Perf (Step 2b)

- `router spawn P1_CODING`: 113.1ms ± 7.2 warm (n=20, hyperfine),
  ~137ms cold-ish. Nothing a user notices; NO PERF row.
- `/api/ui/quota` (92 windows, 325k-row ledger scan bounded at 200k):
  10.7ms ± 0.8. Registry browse (1974 models): 9.7ms ± 0.5.
- Install leg 36s wall on the bunker; 4.6s venv+install on the control
  host; seed 15.9s.

## Housekeeping

- All state writes stayed in the scratch clone / scratch data home; zero
  writes to ~/.hermes/model-router or ~/.local/share/task-router (their
  15:29 mtimes are the fleet's own :9092 server and a sibling lane's guard
  run, confirmed by process census).
- Board rows filed: TR-324, TR-325, TR-326 (+ events.jsonl recurrence for
  QA-TR-FRESH-001). dogfood-log.md row appended.
