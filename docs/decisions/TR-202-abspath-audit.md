# TR-202 — `abspath(__file__)` audit of the task-router runtime modules

Date: 2026-09-29 · Worktree: `wt/TR-202` (task-router) · Method: live execution, not code reading.

## The bug class

Most runtime tools are installed at `~/.hermes/scripts/` as SYMLINKS into the
checkout (`scripts/sync_runtime.sh`, topology 1, TR-004). When Python execs a
script through a symlink, `__file__` is the LINK path while `sys.path[0]` is the
REAL directory (mechanism measured 2026-09-29 with a probe script through a
scratch symlink). So:

- `os.path.realpath(__file__)` (and `Path(__file__).resolve()`) → the real tree. Correct.
- `os.path.abspath(__file__)` → `~/.hermes/...`. For any repo-derived path that
  is the WRONG TREE — on this host `~/.hermes/data/tables`,
  `~/.hermes/registry.json`, `~/.hermes/.coding-hermes/board/` and
  `~/.hermes/scripts/drivers/` do not exist (censused 2026-09-29).

`provider_health_probe.py` was fixed for this same class earlier (candidate
order: env override > repo > live; `realpath` for `_REPO`). TR-202 extends the
audit to every runtime module.

## Live-install census (AC 1) — `ls -l ~/.hermes/scripts/`, 2026-09-29

All 31 installed task-router runtime files were censused against the repo's
`scripts/`. Resolution used by each module's `__file__` sites, and the live
install kind:

| Module (scripts/) | `__file__` resolution | Live install | Resolves to (live exec) |
|---|---|---|---|
| router_clinepass.py | abspath → **FIXED** to realpath | SYMLINK → main tree | before: `~/.hermes` (WRONG) / after: repo |
| router_gaps.py | abspath ×2 → **FIXED** to realpath | SYMLINK → main tree | before: `~/.hermes` (WRONG) / after: repo |
| router_modelsdev.py | abspath → **FIXED** to realpath | SYMLINK → main tree | before: `~/.hermes` (WRONG) / after: repo |
| router_plan_sweep.py | abspath → **FIXED** to realpath + env override added | SYMLINK → main tree | before: `~/.hermes` (WRONG) / after: repo |
| router_pricing.py | `_HERE` realpath (ok) + `_REPO` abspath → **FIXED** | SYMLINK → main tree | before: `_REPO`=`~/.hermes` (WRONG) / after: repo |
| router_probefix.py | abspath → **FIXED** to realpath | SYMLINK → main tree | before: `~/.hermes` (WRONG) / after: repo |
| router_server.py | `Path.resolve()` module level (ok); abspath ×2 in-function (:643 board, :3030 drivers) → **FIXED** | SYMLINK → main tree | before (under link-path `__file__`): `~/.hermes` + missing `drivers/` (WRONG) / after: repo |
| router_seed.py | realpath module level (ok); abspath at :522 sibling lookup → **FIXED** to realpath | SYMLINK → main tree | before: worked only because the sibling was ALSO a symlink in the link dir (latent, and reads the wrong tree from a worktree run) |
| provider_health_probe.py | realpath + candidate order (TR-CI fix) | COPY | repo-independent; state paths by env/home — correct |
| fleet-cooldown-policy.py | `realpath(__file__)` + expanduser sidecar path | COPY | `~/.hermes/scripts/fleet-cooldown-policy.py` — the running copy itself; correct by design (SCHED-PERF-006 sidecar hash) |
| router_health.py | `Path.resolve()` | COPY | `~/.hermes/...` IF exec'd directly — but the module has NO `main()`/CLI: it is imported in-process only, by `router_server.py` (which runs from the repo via systemd `task-router-server.service` / `task-router-proxy.service`, `ExecStart=/usr/bin/python3 /home/kara/task-router/scripts/router_server.py`). No direct-exec consumer exists → not broken; NOT changed (fix-only-proven-broken rule). Noted as a trap: any future cron that execs the copy directly would need this file converted to candidate order first. |
| router_validate.py | realpath module level + `HEAL_SCRIPT` realpath (:236) | SYMLINK (differs from sync_runtime.sh's copy list — live drift, harmless: symlink resolves to the same repo file) | repo — correct |
| router-data-quality.sh | n/a (bash; `cd /home/kara/task-router` hardcoded) | COPY | invokes the six modules with relative `scripts/...` paths from the MAIN tree — abspath was correct there, which is why the cron never saw the bug |
| router_spawn.py | realpath (:115) | SYMLINK → main tree | repo — correct |
| router_circuit.py | none | SYMLINK | n/a — correct |
| router_learn.py | none | SYMLINK | n/a — correct |
| router_ledger.py | none | SYMLINK | n/a — correct |
| router_quota.py | none | SYMLINK | n/a — correct |
| router_maintain.py | realpath (:53, :65) | SYMLINK → main tree | repo — correct |
| router_modelsdev.py — see fixed row above | | | |
| router_diff.py | realpath (:32) | SYMLINK | repo — correct |
| router_estimate.py | realpath (:44) | SYMLINK | repo — correct |
| router_metrics.py | realpath (:41) | SYMLINK | repo — correct |
| router_status.py | realpath (:39) | SYMLINK | repo — correct |
| router_refresh_resume.py | `Path.resolve()` (:40, :46) | SYMLINK | repo — correct |
| router_health_probe.py | none | SYMLINK | n/a — correct |
| router_proxy_stats.py | none | SYMLINK | n/a — correct |
| router_ui_data.py | `Path.resolve()` (:28) | SYMLINK | repo — correct |
| router_web.py | `Path.resolve()` (:25) | SYMLINK (systemd `task-router-web.service` execs the live link; cwd = main tree) | repo — correct |
| router_rank_audit.py | realpath (:41, :224) | SYMLINK | repo — correct |
| router_release_backfill.py | realpath (:38, :284) | SYMLINK | repo — correct |
| router_probe_ingest.py | realpath (:38) | SYMLINK | repo — correct |
| router_probe_run.py | realpath (:46) | SYMLINK | repo — correct |
| proxy_acceptance.py | `Path.resolve()` (:35) | SYMLINK | repo — correct |
| test_fleet_cooldown_policy.py | abspath (:24) | COPY | correct — the copy is meant to test the live copy in place (test harness, not a path consumer) |
| Repo-only tools (NO live install — abspath is correct there, run from a checkout): ci_gate_check.py, gen_modelsdev_silence_rules.py, outcomes_averages.py, plan_effective_backfill.py, proxy_e2e.py, router_audit.py, router_chain_run.py, router_classify.py, router_jev.py, router_lifecycle.py, router_muse_code.py, router_outcomes.py (abspath :32/:694 — kept, repo-only), router_outcomes_freshness.py, router_pricing_audit.py, router_tier_coverage.py, router_trapfix.py | | ABSENT from `~/.hermes/scripts/` | n/a |

`task_router/cli.py` derives `__file__` with abspath but only as the LAST
candidate of `_resolve_repo_root()` (env override → cwd walk-up → file walk-up →
historical fallback, docstring cites the 2026-09-27 site-packages measurement) —
already the provider_health_probe.py pattern, correct under every topology.

## Live-run proof (AC 2) — exec through the live symlink path, 2026-09-29

Method: `runpy.run_path('<install-path>', run_name≠'__main__')` = a real exec
through the link minus `main()`; constants printed are the module's own
computations. Failures quoted verbatim.

Before (live `~/.hermes/scripts/...`, all six broken modules — same shape):

```
===== router_gaps.py (via /home/kara/.hermes/scripts/router_gaps.py)
  _REPO = /home/kara/.hermes
      exists=True
  DATA_DIR = /home/kara/.hermes/data/tables
      exists=False
===== router_clinepass.py  … _REPO = /home/kara/.hermes · DATA_DIR = ~/.hermes/data/tables [exists=False]
===== router_modelsdev.py  … same wrong tree
===== router_plan_sweep.py … same wrong tree
===== router_pricing.py    … _HERE = /home/kara/task-router/scripts (ok) · _REPO/DATA_DIR = ~/.hermes (WRONG)
===== router_probefix.py   … DATA_DIR = /home/kara/.hermes/data/tables [exists=False]
===== router_server.py inline sites evaluated under the link-path __file__:
  :643 _repo = /home/kara/.hermes ; board tasks.jsonl exists = False
  :3030 drivers dir = /home/kara/.hermes/scripts/drivers exists = False
```

Functional damage, quoted (HEAD code exec'd through a symlink, neutral cwd):

```
$ router_plan_sweep.py          → "no flat_subscription providers — nothing to sweep"   (read ZERO rows; exit 0)
$ router_gaps.py --json         → {"total_models": 0, "gapped": []}                     (silent empty read; exit 0)
```

Not every consumer of the fixed modules was blind — the
`model-registry-data-quality` pipeline (`router-data-quality.sh`, a COPY) `cd`s
to the main tree first, so its abspath resolved correctly; the bug hit any
caller that exec'd the live SYMLINKS (manual ops, one-off cron lines, future
consumers of sync_runtime.sh's symlink topology).

The repo-only six (`router_audit`, `router_chain_run`, `router_classify`,
`router_jev`, `router_muse_code`, `router_pricing_audit`) have NO live install:

```
===== router_audit.py (live run via /home/kara/.hermes/scripts/router_audit.py)
  module-level RAISED: FileNotFoundError: ... No such file or directory: '/home/kara/.hermes/scripts/router_audit.py'
```

— their `abspath(__file__)` resolves inside whatever checkout they run from,
which is correct for a repo-only tool; per the fix-only-what-is-proven-broken
rule they are NOT changed.

## Fixes (AC 3) — same recipe as provider_health_probe.py

Eight sites in seven files, `abspath(__file__)` → `realpath(__file__)`:
`router_clinepass.py:37`, `router_gaps.py:29,36`, `router_modelsdev.py:79`,
`router_plan_sweep.py:29`, `router_pricing.py:57`, `router_probefix.py:53`,
`router_seed.py:522`, `router_server.py:643,3030` (two files carry two sites
each). `router_plan_sweep.py` also gained the `ROUTING_DATA_DIR` env override so
all sibling tools share the same override contract. No candidate-order fallback
was needed: unlike provider_health_probe.py (a COPY whose repo-relative default
was wrong in CI), every fixed module is a SYMLINK whose only consumers exec the
repo tree — repo default is already correct in CI, in the repo, and from the
live link once realpath resolves it.

After (fixed files exec'd through a scratch symlink dir — identical mechanics to
the live install; the live symlinks themselves keep serving main until merge):

```
===== router_gaps.py (via <symlink>)   _REPO = <worktree> · DATA_DIR = <worktree>/data/tables [exists=True]
===== router_clinepass.py  … _REPO + DATA_DIR inside the real tree, exist=True (all six)
===== router_probefix.py   … DATA_DIR inside the real tree; HEALTH_JSONL still the live state file (env/home by design)
$ router_plan_sweep.py  → "sweep: nothing to disable for ['clinepass', 'grok-build', 'minimax', 'ollama-cloud', 'stepfun', 'xkiro', 'xkiro-2']"  (reads 7 providers, not zero)
$ router_probefix.py --dry-run → "probe 404 scan: 33 invalid-model-id failure(s) in last 48 runs" (tables found)
```

Honest negatives from the same battery (recorded so nobody re-chases them):
`router_modelsdev.py mappings` printed identical output pre- and post-fix —
`load_mappings()` does not ride on `_REPO`; and `router_seed.py`'s derived
probe-key ingestion (`_derived_keys`, 22 entries) worked pre-fix by symlink
coincidence — fixed anyway (it would have silently degraded to the hand list
the day `router_probe_ingest.py` became a copy, and it reads the wrong checkout
from any worktree run).

## Regression guard

`tests/test_symlink_path_resolution.py`:
1. Every FIXED + CONTROL module exec'd through a scratch symlink must resolve
   all its repo-derived constants inside the real tree, with the
   must-exist set actually existing (behavioural, per-module).
2. Mechanism self-test: the same probe flags a synthetic abspath module and
   passes a synthetic realpath module (the technique cannot go silently blind).
3. Source guard: no file that `sync_runtime.sh` installs may contain
   `abspath(__file__)` at all (repo-only tools exempt — abspath is correct
   there). This is the tripwire that catches the next module written the old way.

Full suite: `~/.hermes/venvs/board/bin/python3 -m pytest -q tests/ -x` green on
the fixed tree (see commit / worker report for the run counts).
