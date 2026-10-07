# TR-205 — pagination mirroring: verification record (2026-10-07)

## Premise correction (worker finding)

The dispatch brief stated the deployed live copy
(`~/.hermes/scripts/fleet-cooldown-policy.py`) was the hardened one and
ordered it mirrored INTO the repo. The opposite was true on disk:

| | repo `scripts/fleet-cooldown-policy.py` | live `~/.hermes/scripts/fleet-cooldown-policy.py` |
|---|---|---|
| HEAD revision | e5730f1 (2026-10-02 16:59) | sidecar-pinned `38e20995…` (pre-cb4c4fb era) |
| bounded pagination | `PROJECTS_MAX_PAGES=20`, `for range` loop (L85/L335, cb4c4fb 09-30) | `while True` unbounded (L277) |
| SCHED-GAP-1602 X-Operator-Token | present (L75) | present |
| retired-apply guard (owner 2026-10-02) | present (L485, commit 849359d) | ABSENT — live `--apply` still carries the PUT writer |
| admission-law report | present (L576+) | absent |

Executing the literal `cp` would have deleted the writer retirement and the
bounded loop from the canonical repo and reintroduced an unbounded loop.
It was not executed; the repo file is unchanged by this task (zero diff).

## Follow-up recommended (foreman/owner, not done by this task)

The LIVE copy is the stale side: pre-TR-205 pagination and still armed with
the `--apply` correction writer that 849359d retired on 2026-10-02. No cron
invokes it (`fleet-auto-heal.py` calls `fleet-sync.py --write`; zero
`jobs.json` references), so it is human-invocation-only risk. Deploying the
hardened copy needs care:

1. The live test files (`test_fleet_cooldown_policy.py`,
   `test_fleet_cooldown_policy_deploy_hash.py`,
   `test_fleet_cooldown_policy_reduce_guard.py`) assert behavior of BOTH
   eras (e.g. `test_apply_refuses_on_empty_map_with_many_projects` expects
   exit 2 from an `--apply` the hardened copy refuses with exit 3) — migrate
   them in the same move.
2. `sync_runtime.sh` case 2 will SKIP+FAIL while the sidecar still pins the
   old hash ("live matches canonical sidecar, REPO DIFFERS") — deployment
   requires `--update-canonical` (or an explicit sidecar update) after the
   copy, then a `sync_runtime.sh` re-run to prove drift zero.

## Live consumers paginated this task (all in ~/.hermes/scripts)

`?limit=500&offset=N`, accumulate, stop on envelope `total` covered or
offset stall, bounded at 20 pages, matching each file's fetch style:

fleet-board-audit.py, fleet-cooldown-audit.py, fleet-cost-digest.py,
fleet-gitreins-audit.py, fleet-git-rewrite.py, dogfood-pick.py,
standin-pick.py, standin-report.py, daily_telemetry_extract.py,
test_fleet_enabled_consistency.py (already paginated; bounded + warn).

Not touched (already paginated per brief): fleet-auto-heal.py, fleet-sync.py.

## Live evidence (2026-10-07)

- Envelope: `GET /api/v1/projects?limit=500&offset=0` → total=594, page1=500 rows.
- `api_get_all_projects()` (repo module, read-only): **594 lanes accumulated,
  594 unique, 0 duplicates, tail = wojons-mythos-*/zz-* probes** — >= total.
- Consumer live runs (read-only / designed side effects only):
  fleet-cooldown-audit rc=0 silent (watchdog clean), standin-pick --dry-run
  real picks, standin-report enabled=387, test_fleet_enabled_consistency
  "594 fleet.toml projects match API (387 enabled)", daily_telemetry_extract
  fleet.json total=594/enabled=387/projects=594, fleet-gitreins-audit real
  findings rc=0, board-audit/cost-digest/dogfood-pick/git-rewrite
  import-level fetch calls all 387-594 rows, no errors.
- One worker defect caught by live run and fixed in place: standin-report.py
  referenced MAX_PAGES without defining it (swallowed by its `except`);
  after fix the report counts 387 enabled.

## Verification

- `python3 -m py_compile` — all 10 touched files OK.
- Repo smoke suite `pytest -q tests/` — see commit-day run output (1793 tests,
  TR-205 file 10/10 passed standalone).
- `./scripts/gitreins-guard-tests.sh` — full battery under `timeout 2400`,
  result recorded in the closing report.
- No writer mode executed anywhere; the repo policy script was not modified.
