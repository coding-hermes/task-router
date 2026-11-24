# Dogfood run 47 — 2026-11-24 — diagnostics (tick task-router-dogfood-2026-11-24-19-47-57)

How the run was built, what went wrong, the right way. Companion to
`2026-11-24-run47-integration.md`.

## Rig

- Consumer: scratch clone `--no-hardlinks` of `/home/kara/task-router`
  (HEAD `3231e966`, == `origin/main`, fetch-verified at fire time) + `uv venv`
  + editable install + duckdb. Isolation env: `TASK_ROUTER_HOME` and
  `ROUTING_NS` pointed at the scratch data home, `ROUTING_ALLOW_FLEET_MIRROR=0`
  — seed's unset-NS guard resolves to the data-home scratch ns whenever a real
  DuckBrain ns exists on the box, but exporting explicitly is the version that
  documents itself.
- All timings via `/usr/bin/time -o/-a -o`, product runs from the clone's own
  venv. Nothing in `~/.hermes/model-router`, `~/.local/share/task-router`, or
  the live DuckBrain routing ns was written; the live repo's working tree was
  not touched (untracked sibling files there are not this run's).

## Errors hit, in order, and what taught what

1. **`python3 -c` inline probes are a command-gate refusal on this surface.**
   First compound dies with BLOCKED; the sanctioned shape is a staged script
   run by path (`probe-*.py`). Rebuilt all probes as staged files — same
   measurements, no gate dance.
2. **`router validate` worked from `/home/kara` but died from the clone's
   parent directory** — `router: dispatch failed: No module named
   'lifecycle_gate'`. Not a venv fault, not a wheel fault: `task_router/cli.py`
   `_resolve_repo_root()` walk-up accepts *any* tree that owns
   `scripts/router_spawn.py`, and `/home/kara/.hermes` owns one. The probe
   (`probe-validate.py`) pinned `REPO: /home/kara/.hermes` — the Hermes mirror
   copy of the product scripts, which has drifted (36 router/registry scripts
   vs 79 in the product; `lifecycle_gate.py` absent), so the mirror's
   `router_validate.py` fails its own sibling import. => TR-1000976681059308.
   Fleet lesson: cron cwd is `~/.hermes`; any `router` call there grades the
   mirror.
3. **Warm spawn 5.1–5.75 s where run 34 measured 113–161 ms.** Hypothesis 1
   (live ledger at `~/.hermes/model-router`) was WRONG — that ledger is 4 KB
   now; a scratch `ROUTER_STATE_DIR` made it *slower* (6.3–7.4 s) because the
   outcomes store lives in the REPO (`router_outcomes.py:37`
   `data/state/outcomes.jsonl`), not the state dir. Hypothesis 2 (store-sync
   racing) checked: sync stamp completed 15:05:36, measurements 15:05+ with
   TTL 30 min, so no resync was in flight. The A/B that settled it:
   `ROUTING_OUTCOMES_FILE` → empty file = 0.34–0.39 s; fresh clone with no
   store = 0.29–0.30 s. cProfile: 464,766 `json.loads` = 4.76 s of the run.
   => PERF-2. The caller breakdown needed a second probe
   (`print_callers`), because `json.loads`' own row does not name callers in a
   top-N table.
4. **Where the clone's store came from**: spawn itself imports it
   (`router_spawn.py:5923` → `router_store_sync.sync_if_due()`, TR-116's
   "no cron" doctrine). So a fresh clone on a fleet box self-inflates to the
   live store's size and inherits PERF-2's cost on its second resolve.
5. **Seed behaved** (79.95 s, rc 0, 14/14 valid): run 42's
   SATISFIABILITY-REFUSED-but-wrote-anyway defect is gone at this tip; the
   clone's only post-seed modification was `data/state/seed_cache/manifest.json`.
   Seed's stderr still prints a confusing pair (`seed cache: stored …` then
   `entry rejected — derived by different code (fingerprint …  != …)`); cosmetic,
   not filed.
6. **First-spawn exclusions name hop 2** `deepseek-duckbrain-sync`
   `policy-gate-missing-row` — the bootstrap quota sample does not carry that
   provider. Honest-but-wrong-by-omission => TR-1000976681059309.
7. **Install leg**: `bunker-qa.sh launch /home/kara/task-router --keep` →
   PREFLIGHT FAIL disk 96% > 94% (rc 2, 0.88 s), while `bunker metrics` reads
   137.7/149.9 GB. Two views, neither adjusted, launcher fail-closed honored
   => QA-TASKROUTER-27 (SKIPPED-install-bunker).

## The right way, for the next agent

- Do spawn/store perf from a scratch clone with an explicit
  `ROUTING_OUTCOMES_FILE` control arm — it is the one line that separates
  "product is slow" from "the store is heavy".
- Never run `router` from a cwd whose ancestors include `~/.hermes` unless you
  know which tree you are grading; add `TASK_ROUTER_REPO=<checkout>` if in
  doubt (it outranks the walk-up).
- Keep `TASK_ROUTER_HOME` + `ROUTING_NS` explicit for any scratch dogfood; the
  unset-NS guard is clever but the explicit export is evidence.
- Full-suite gitreins guard (test_mode full, ~917 s in CI) runs on every
  commit in this repo — budget the closeout, don't parallelize board commits
  with the same index.
