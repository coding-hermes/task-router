# Run 46f diagnostics — 2026-11-15T20:15Z — first real use of `router chain-run`

Fire 46f of tick `task-router-dogfood-2026-11-15-15-59-32`. Measured HEAD `b6731033` (`product_tree_since_46e:
records-only` — `git diff --name-only 5ce46384..HEAD` lists only `board/events.jsonl`, `board/tasks.jsonl`,
`dogfood-log.md`, one evidence file and two dogfood docs).

Surface chosen because it had never been used: `router chain-run` has **0** mentions in
`.coding-hermes/dogfood-log.md` and **0** use rows on the board, and it is the one command a user wires into
their own agent loop. Everything below was run from `/home/kara/.hermes/cache/scratch/tr-chainrun-46f` with an
isolated `TASK_ROUTER_HOME` (seeded `registry.json`) so the repo's config was not in play.

## 1. cwd parity — three of four commands are "everywhere", the executor is not

`README.md:632-633` claims: `` `router <cmd>` everywhere; the `scripts/router_*.py` names are the same tools for
repo-relative use.`` Tested literally, no checkout in or above the working directory:

```
router spawn my-project --format json --quiet   -> rc 0
router status --format json                     -> rc 0
router estimate my-project                      -> rc 0
router chain-run my-project --dry-run           -> rc 2
```

```
router: cannot find the task-router checkout.
  looked for: /home/kara/.hermes/scripts/router_chain_run.py
  The scripts/ tools ship with the checkout, not with the installed wheel.
  Fix one of these ways:
    * run `router` from inside a checkout (any subdirectory works), or
    * point at one explicitly: TASK_ROUTER_REPO=/path/to/task-router router chain-run ..., or
    * use the checkout's own entry point: python3 scripts/router_chain_run.py ...
```

The tool is defensible and its error is the best in the surface; the README's blanket "everywhere" is not. Row
`DOC-19`.

## 2. Real walk — the advance path works

Caller stub that fails only at `ROUTER_HOP=13` and records the env it was handed. `--max-hops 2`:

| hop | provider/model | rc | outcome |
|----|----|----|----|
| 13 | `xkiro/openai/gpt-6-luna` | 1 | `transport-failure` |
| 14 | `xkiro-2/openai/gpt-6-luna` | 0 | success — walk stops |

Summary `success=true final=xkiro-2`; `ROUTER_HOP` / `ROUTER_PROVIDER` / `ROUTER_MODEL` matched the hop actually
being attempted on both attempts. Failure captured per attempt (`rc`, `latency`, `error` are all present in
`attempts[]`, so the earlier suspicion that they were missing was wrong and was dropped, not filed). 14,050 ms
for two hops where the caller command itself takes 0.019 s.

## 3. Total exhaustion exits 0

`detfail.sh` prints `deterministic error: bad config` to stderr and exits 3. Nothing about it is retryable.

```
--max-hops 5  -> 5 attempts (hops 14-18), all outcome=transport-failure, 25,028 ms, success=false final=null, PROCESS rc 0
--max-hops 1  -> 1 attempt, success=false final=null, PROCESS rc 0
```

A shell consumer cannot tell this run from a success. Row `TR-1000976681059264`.

## 4. Every non-zero exit is "transport-failure"

`scripts/router_chain_run.py:104-106`:

```python
ok = (rc == 0)
...
'outcome': 'success' if ok else 'transport-failure',
```

A deterministic exit 3 is classified exactly like a dead provider, so the walk replays it across every remaining
hop; with the resolved chain at 71 hops and ~4-5 s per attempt, a misconfigured command costs ~5-6 minutes before
the failure is reported, and there is no `--stop-on-rc`. Row `TR-1000976681059265`.

## 5. Perf — the bookkeeping is 300x the work

Same command, same directory, isolated home:

| invocation | wall |
|---|---|
| `--cmd 'bash attempt.sh' --max-hops 1` | 6,336 ms (caller 0.019 s) |
| warm repeat 1 hop | 6,882 ms |
| warm repeat 1 hop | 6,438 ms |
| `--max-hops 2` | 14,050 ms |
| `--cmd 'bash detfail.sh' --max-hops 5` | 25,028 ms |
| control: `--dry-run --max-hops 1` | 413 ms |

cProfile over `scripts/router_chain_run.py`, 2 failing attempts: 9.897 s total, **954,560** calls to
`json/__init__.py:299(loads)` costing **6.091 s** cumulative — ≈475k per attempt, which equals the line count of
`data/state/outcomes.jsonl` (413,629,614 bytes / 475,349 lines at the start of this fire; 475,361 after its own
5 appended rows). The loop is `scripts/router_data_cache.py:471-477` (`_default_load`, one `json.loads` per JSONL
line) entered from `scripts/router_spawn.py:1851` (`load_outcome_stats`) — re-read and re-parsed per hop, even
though every hop in `router spawn my-project --format json` already carries an `outcomes` block
(`window_h`, `stats_source`, `n_samples`, `avg_wall_time_s`, `success_rate`).

Not counted in the per-hop number: `time.sleep` 2.484 s cumulative over 8 sleeps, from the background cache
`_poll_loop` (`scripts/router_data_cache.py:601`); it overlaps the main thread and is not proven additive.
Row `PERF-1`.

## 6. Ledger integrity

While the walks appended their rows, stderr said: `append_rows: skipped malformed line 436576 / 436577 in
data/state/outcomes.jsonl`. Verified rather than trusted: `436575` OK, `436576` MALFORMED, `436577` MALFORMED.
Line 436577 begins `{"source_sy{"source_system": "hermes"` — a truncated router-proxy record prefix spliced onto
a complete hermes row, the signature of two writers interleaving a ~2.7 KB row. The loss is 2 of 475,361 rows and
predates this tick (this fire's own rows are the 5 chain-run ones); the risk is the pattern, and the count is only
ever surfaced as stderr noise during an unrelated append. Row `TR-1000976681059266`.

## 7. Install leg

Bunker leg **not re-run** this fire — the fresh-clone leg was measured by fire 46e on this same product tree
(4 s clone + 5 s `uv pip install -e '.[dev]'`, `router --help` rc 0), and the product tree is unchanged since.
`bunker status --server bunker-mvp`: ONLINE, 3/50 agents, 0 orphan users/homes, 20 orphan keys, disk
83 % (125.0/149.9 GB), inside the 94 % admission gate. Recorded in the evidence artifact; not a silent pass, and
not a SKIPPED row because the leg did run and has a receipt from 46e on this exact tree.

## 8. Board writes verified

`boardctl create` appended 5 rows with `--evidence-run-id task-router-dogfood-2026-11-15-15-59-32-46f` and the
sha256 of this fire's evidence artifact. `grep -c '"id": "<id>"'` = **1** for each of `TR-1000976681059264`,
`TR-1000976681059265`, `TR-1000976681059266`, `PERF-1`, `DOC-19`. `boardctl validate` on the working tree:
`RESULT: FAIL (455 error(s), 590 warning(s))`; baseline `origin/main` board in a scratch dir:
`FAIL (455 error(s), 591 warning(s))` — **same error count**, so the five rows added zero errors, and no error
line names any of them. The 455 are the board's own `events.jsonl` id-ordering debt, already owned.
