# TR-202 re-pass — `abspath(__file__)` / hardcoded-path audit (2026-10-07)

Worktree `wt/TR-202` · follow-up to `docs/decisions/TR-202-abspath-audit.md`
(2026-09-29). Method: mechanical grep census over `scripts/` + `tests/`
(`abspath(__file__)`, `realpath(__file__)`, `Path(__file__).resolve()`,
`/home/kara`), then a per-hit classification against the live-install topology
(`scripts/sync_runtime.sh`), not code reading.

## TL;DR

The 2026-09-29 pass fixed the abspath(__file__) SYMLINK class for the
live-installed runtime tools and added two guards
(`tests/test_symlink_path_resolution.py`: per-module symlink-exec probe +
source-level tripwire). This re-pass verified all 9 prior fixes are still in
place and extended the audit to the class that census left behind:
**hardcoded `/home/kara/...` data paths** in the state-side tools — wrong tree
on any other user, host, second checkout, or worktree, exactly like the
symlink bug but by a different mechanism. 9 files / 13 sites fixed; the
intentional hits are documented below.

## Census and classification — scripts/

Legend: **FIXED-2026-10-07** (this pass) · OK-symlink (prior round, verified)
· OK-repo-only (abspath correct: never live-installed) · INTENTIONAL
(documented, no change).

### Class A — `__file__` resolution (symlink class, live-install scope)

| File:line | Form | Live install | Classification / action |
|---|---|---|---|
| router_clinepass.py:38,46 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_gaps.py:31,39 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_modelsdev.py:79,86 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_plan_sweep.py:29,37 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_pricing.py:50,60 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_probefix.py:55 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_seed.py:19,629 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| router_server.py:683,3272 | realpath | SYMLINK | OK-symlink (fixed 09-29) |
| cost_backfill.py:26 | realpath | — (no live install) | **FIXED-2026-10-07** (was hardcoded, see class B) |
| dummy_scheduler.py:44 | realpath | — | **FIXED-2026-10-07** |
| proxy_smoke.py:23 | realpath | — | **FIXED-2026-10-07** |
| provider_health_probe.py:88,93 | realpath | COPY | OK (TR-CI lineage; candidate order env>repo>live) |
| fleet-cooldown-policy.py:422 | realpath | COPY | OK — SCRIPT_PATH sidecar-hash identity, by design (SCHED-PERF-006) |
| policy_gate_audit.py:27,41 | realpath | SYMLINK | OK |
| router_circuit.py:81 / router_maintain.py:53,66 / router_diff.py:32 / router_estimate.py:44 / router_metrics.py:41 / router_status.py:39 / router_validate.py:46,236 / router_rank_audit.py:41,224 / router_release_backfill.py:38,43,300 / router_probe_ingest.py:38 / router_probe_run.py:46 | realpath | SYMLINK | OK |
| router_ingress.py:63,107 / router_refresh_resume.py:46 / router_health.py:28,68 / router_ui_data.py:28 / router_web.py:25 / proxy_acceptance.py:35 / proxy_e2e.py:48 / derive_projects_seed.py:43 / outcomes_averages.py:46 / provider_health_dashboard.py:28 / provider_health_summary.py:19 / router_clinepass etc. | `Path.resolve()` / realpath | SYMLINK or repo-only | OK |
| board_row_levels.py:200 · board_task_intake.py:68 · ci_gate_check.py:42 · data_null_census.py:40 · fill_null_reasons_tr197.py:50 · fix_overlay_wipes_tr198.py:61,66 · hold_dash_preview.py:6 · migrate_quota_tr206.py:32 · plan_effective_backfill.py:26 · quality_score.py:55 · router_ab.py:50 · router_audit.py:25 · router_chain_run.py:35 · router_classify.py:28 · router_jev.py:35 · router_lifecycle.py:17,23,29,360 · router_muse_code.py:45,47 · router_nohops.py:77,79 · router_outcomes.py:32,164,948 · router_outcomes_freshness.py:31 · router_pricing_audit.py:56 · router_provider_import.py:30,33 · router_quota_readback.py:54 · router_tier_coverage.py:53 · router_trapfix.py:33 · validate_provider_quota.py:351 · verified_outcomes.py:59 · xray_scan.py:5 · drivers/*.py (init:18, base:84, deepseek_harness:33, hermes:18,58, openclaw:42,123, opencode:44,115, pi:28,107) · pricing/helpers.py:44 | abspath | ABSENT from `~/.hermes/scripts/` (verified against both sync_runtime.sh loops) | OK-repo-only — abspath resolves inside whatever checkout runs them, which is correct; exempted by the source guard on purpose |
| test_fleet_cooldown_policy.py:24 | abspath | COPY | INTENTIONAL — the deployed copy of this harness must import the LIVE sibling policy copy in place; realpath would resolve through nothing today but would repoint a future relocated copy at the repo |
| gen_modelsdev_silence_rules.py:10,15 / hold_dash_preview.py:6 / xray_scan.py:5 | bare `dirname(__file__)` | repo-only | OK-repo-only (relative to the invoking checkout) |

### Class B — hardcoded `/home/kara/...` (this pass's fixes)

| File:line (pre-fix) | Was | Now | Why it was a portability bug |
|---|---|---|---|
| cost_backfill.py:21,22 | `LEDGER`/`REGISTRY` = `/home/kara/task-router/...` | repo-relative realpath defaults + `ROUTING_OUTCOMES_FILE` / `ROUTING_REGISTRY` env (shared sibling contract) | any other checkout/worktree silently read+wrote the MAIN tree's ledger, or crashed; backfill even holds a write lock on it |
| dummy_scheduler.py:41,42 | `LEDGER`, `RAW_OUT` hardcoded | repo-relative + `ROUTING_OUTCOMES_FILE` | same wrong-tree read (ledger delta is its amplification metric) |
| proxy_smoke.py:21,22 | `OUT`, `LEDGER` hardcoded | repo-relative + `ROUTING_DATA_DIR` / `ROUTING_OUTCOMES_FILE` | same |
| plan_effective_backfill.py:29,35 | dead global `STORE` (~/.hermes/model-router/../... joinery) + `--store` default `/home/kara/...` | default via `router_outcomes.outcomes_path()` (TR-049 resolver: env → repo gitignored state); dead global removed | the argparse default bypassed the resolver router_outcomes already provides |
| router-data-quality.sh:14 | `cd /home/kara/task-router` | `cd "$(dirname "${BASH_SOURCE[0]}")/.."` | a copied/relocated pipeline silently ran the MAIN tree (measured: pre-fix copy exec'd the main-tree syncs, exit 0) |
| router_refresh_resume.py:63 | phase0 `python3 /home/kara/.hermes/scripts/reconfigure.py` | `_find_reconfigure_script()`: checkout copy → live install (`~`-relative) | worktree/cron runs healed the wrong tree; foreign host pointed at a path that cannot exist |
| router_maintain.py:98,99 | `ROUTING_NS`/`TASKROUTER_NS` defaults = `/home/kara/duckbrain/...` | `os.path.expanduser('~/duckbrain/...')` | the exact mirror-path shape TR-045/TR-048 removed from router_seed.py's defaults; env overrides still win (byte-identical on this box) |
| router_pricing_audit.py:63 | `STATE_DB` default `/home/kara/.hermes/state.db` | `os.path.expanduser('~/.hermes/state.db')` | same class as maintain's `BOARD_PY`/`SPOT_CHECK` expanduser idiom |
| fresh_clone_smoke.sh:22 | default interpreter `/home/kara/.hermes/venvs/board/bin/python3` | probe `${HOME}/.hermes/venvs/...` → PATH python3 (PYTHON= still wins) | the FRESH-CLONE smoke exited 2 on a stranger's clone before running a single check |

### Class B — intentional / documented (no change)

| File:line | Value | Why intentional |
|---|---|---|
| router_seed.py:41 | `_FLEET_MIRROR_NS = '/home/kara/duckbrain/namespaces/routing'` | the TR-048 audited contract itself: an off-default opt-in constant guarded by tests/test_seed_ns_guard.py (bare seed must NOT write it); changing it would weaken the guard's meaning |
| gitreins-guard-tests.sh:37-39 | board-venv probe paths | already the documented probe-then-PATH-fallback pattern (comment :32-34); fallback makes it work in CI/other hosts |
| task-router-web.service:3,8,13,16,17 · provider-health-web.service:3,9,10 · systemd/coding-hermes-scheduler.service.orig-2026-09-26:8-48 | absolute paths | systemd unit files are per-host deploy artifacts by nature (no expanduser); they document THIS host's wiring |
| test_fleet_cooldown_policy.py:67,78,79,218,241 | fixture workdirs | synthetic rows for the live-deploy harness; never resolved as paths in this repo |

## Census — tests/ (summary, not per-line)

- ~120 `abspath(__file__)`-derived `REPO` constants: every one computes the
  repo root from the TEST FILE's own location, which is CWD-independent by
  construction — correct; no action.
- `Path(__file__).resolve()` variants (test_board_task_intake,
  test_doc_parity, test_health_plane, test_server, ...): resolve symlinks,
  strictly more portable — correct.
- `/home/kara` literals: (a) the `PY = board-venv-if-exists-else-sys.executable`
  probe pattern (test_contract:18, test_health_probe:28, test_metrics:16,
  test_probe_flags:15, test_probe_only_merge:24, test_probefix_classification:20,
  test_tr046_polish:37, test_tr199:36, test_tr233:30, test_tr247:30,
  test_ux_commands:35, test_validate:15, test_web:23) — guarded probes,
  INTENTIONAL; (b) test_seed_ns_guard.py:48,147,164 + test_cli_paths.py:277,415 —
  the fleet-mirror constant and its "never this path" assertions are the THING
  UNDER TEST (TR-045/TR-048); INTENTIONAL; (c) docstrings/comments. No
  test resolves a data path through a hardcoded user path.

## Regression coverage added

`tests/test_tr202_abspath_portability.py` (13 tests):

1. State-tool defaults (`cost_backfill`, `dummy_scheduler`, `proxy_smoke`,
   `plan_effective_backfill`) resolve INSIDE this checkout from a FOREIGN CWD,
   via runpy exec of the real module (RED-proven: reverting cost_backfill
   reproduces `/home/kara/task-router/...` verbatim).
2. Full-loop probe: `plan_effective_backfill.py` dry-run + `--apply` against a
   scratch store (`ROUTING_OUTCOMES_FILE`) from a foreign CWD prices a row from
   the repo's own models table and writes ITS store with backup — never a
   hardcoded path.
3. `router-data-quality.sh`: a relocated copy must run every pipeline step in
   ITS OWN repo (stub interpreter records CWDs — side-effect-free in both
   directions; RED-proof captured the pre-fix version running 7 steps in
   `/home/kara/task-router`).
4. `router_refresh_resume`: reconfigure resolution prefers the checkout copy,
   the foreign-host fallback is `$HOME`-relative (never `/home/kara`), and the
   phase0 argv carries the resolved constant.
5. `router_maintain` ns defaults are expanduser-shaped AND env-steered (fresh
   subprocess, since they bind at import); `router_pricing_audit` STATE_DB is
   expanduser.
6. The three newly repo-relative tools are registered in the
   test_symlink_path_resolution.py FIXED set, so the existing live-symlink
   guard covers them from now on.

## Verification

- `pytest tests/test_tr202_abspath_portability.py tests/test_symlink_path_resolution.py`
  → 30 passed (RED proofs: cost_backfill revert → hardcoded-path failure;
  router-data-quality.sh revert → 7 steps recorded in `/home/kara/task-router`).
- Full suite: `~/.hermes/venvs/board/bin/python3 -m pytest -q tests/` → green
  (counts in the commit / worker report).
- RED-proof side-effect note: the FIRST (pre-hardening) bash RED probe really
  executed the main-tree pipeline; the three dirtied generated tables in
  `/home/kara/task-router/data/tables/` were restored with
  `git checkout --` and re-verified clean before proceeding; the shipped test
  is stub-interpreter-based and cannot repeat that.
