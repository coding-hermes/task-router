# task-router ops tick — 2026-11-06-00-02-50 (re-fire)

Lane: `task-router-ops` (cooldown 86400s, workdir stand-in `~/.hermes/stand-in/pm/task-router`,
board bind-mounted to the canonical checkout — `stat -c %i` equal on both paths: 22939329).
Canonical root: `/home/kara/task-router`, remote `git@github.com:coding-hermes/task-router.git`.

## Tick identity — this is a RE-FIRE of a killed execution

`ticks` row for this exact id: `spawned_at 2026-11-05T19:02:50-05:00`, `completed_at 19:43:03`,
`status=failed`, error `gateway HTTP 502 and exec fallback disabled`. The first pass left **no**
board row naming the tick id, no evidence file, no receipt — it died in transport, not in work.
This pass therefore ran the tick from scratch (nothing to continue, nothing to re-implement).

## Selected row

`TR-REV2-20261027-02` (P1, pending since 2026-10-27) — *serving units are stale: :9092 and :9391
answer with code loaded 2026-10-19*. Selected over the P0s because the mechanism is measured and
the action is one deploy; the standing P0s (TR-355/363/371/375/376) are multi-file features that
belong to a worker, and the top P0 (RELEASE-2026-10-22-001) is owner-gated.

## Pre-state (measured, `pre-state.txt` in tick scratch)

```
:9092  commit 04fdeb242793  stale=true  loaded_source_sha 35182a6ce5763fae  live_source_sha 0641f4dcd8c2cbae
:9391  commit 04fdeb242793  stale=true  loaded_source_sha 35182a6ce5763fae  live_source_sha 0641f4dcd8c2cbae
:9093  process started 2026-10-19 15:42 (never restarted)
pids   514260 (proxy), 514277 (api) started 2026-11-04 22:18:29
HEAD   6e1f4dc, parity with origin/main 0/0, porcelain 0 paths
```

`loaded_source_sha != live_source_sha` on both listeners = ARMED CUTOVER, not the benign
commit-label false alarm: the file on disk is a different code digest from the one booted.

## The deploy contract was unrunnable — three independent breakages, all measured

`make restart-router` is the documented deploy path (TR-255) and had never completed on this host:

| # | defect | measurement | status |
|---|---|---|---|
| 1 | step 0 guard probes the SYSTEM scope (`systemctl is-active`) for units the same target restarts with `systemctl --user restart` | pre-fix rc=4 for 3/3 units; post-fix rc=0 for 3/3 | **fixed** `5ba26d8` |
| 2 | step 4/4 passes `RESTART_VERIFY_URLS` positionally to `router_stale_watchdog.py`, which takes `--url` | positional rc=2 (argparse), `--url` rc=0 with both verdicts OK | **fixed** `48d1617` |
| 3 | step 2 `sync_runtime.sh` aborts FATAL on `fleet-cooldown-policy.py`: live copy matches the SCHED-PERF-006 sidecar, repo differs | `diff` = 301 changed lines; repo has the 2026-10-02 retirement header + TR-205 bounded pagination, live lacks both and carries `_canonical_satellite_cadence` which **never existed in repo history** (`git log -S` empty) | **NOT fixed** — pre-existing, non-serving file, filed as `TR-CONTRACT-20261106-01` |

Defect 3 is not a deploy-scope decision this lane may take: the sidecar pins a *pre-retirement*
lineage of a scheduler-owned script, and the two copies diverged in both directions. It is
recorded, not clobbered.

## Deploy executed and verified

Because step 2 aborts on a file the serving modules do not import (`grep` for
`fleet-cooldown\|fleet-cooldown-policy` in `router_server.py`/`router_web.py`/`router_health.py`
is empty) and every serving module is a symlink into the tree, the restart was run through the
operator fallback the target itself prints, after the two guard/verify fixes were on the tip:

```
for u in task-router-server.service task-router-proxy.service task-router-web.service; do systemctl --user restart $u; done
```

Post-state (all literal, `watchdog.json` in tick scratch):

```
:9092  commit 48d1617a5272  stale=false  loaded_commit=48d1617a5272  repo_commit=48d1617a5272  loaded_source_sha==live_source_sha
:9391  commit 48d1617a5272  stale=false  loaded_commit=48d1617a5272  repo_commit=48d1617a5272  loaded_source_sha==live_source_sha
:9093  http=200 (first restart since 2026-10-19)
router_stale_watchdog.py --url http://127.0.0.1:9092 --url http://127.0.0.1:9391 --json -> rc=0, verdict OK/OK
systemctl --user is-active <3 units> -> active/active/active
```

Caller impact: the proxy was serving 8-9 in-flight agent turns at restart (accepted counter 223
since boot, peak 12); a `systemctl restart` drops those connections. Recorded rather than hidden —
the alternative was leaving main's code undelivered, which is the row's own P1.

## Ops legs (census / uptime / CI)

**Census** `fleet_ops_check.py --project task-router --json` (UNPIPED): `rc=1`, one gap:
`deploy-doc` P3. `kind=library`, `evidence.supervision=["scripts/systemd"]`.

- **`kind=library` is a misclassification, not a clean pass.** Three long-lived listeners run
  from this repo's own scripts (`/proc/514260/cmdline`, `/proc/514277/cmdline` → `scripts/router_server.py`),
  which is the documented "library on a repo whose services are standalone scripts" hole. Judged
  this family on the `service` row set by hand for this tick.
- **`deploy-doc` P3 refuted-ish, not closed.** There is no `DEPLOY*/RUNBOOK/OPERATIONS` doc, but
  the deploy path is `make restart-router` inside `Makefile` + `scripts/sync_runtime.sh` +
  `scripts/router_deploy.py`, and README carries the prerequisites. That is a naming gap, and this
  tick's finding is the stronger one: the path existed and was broken. Not filed separately.

**Uptime** (`ticks`, last 2 days, per lane, cooldown-aware): every non-completed lane in the
family is failing on the SAME class, and that class is fleet-wide — `gateway HTTP 503` 29,
`502` 19, `429` 11 across ALL projects in 24h. Per the ops doctrine these are one outage with one
owner (the gateway/router), not eleven project defects; nothing filed on this board for them.
`task-router-foreman` completed 19 ticks in the window.

**CI** `gh run list --repo coding-hermes/task-router`: the four runs for the TR-381 commits
(`593271f`, `a426f0a`, `b879765`, `6e1f4dc`) were `in_progress` at the start of this tick and
still `in_progress` at close — **UNVERIFIED, not green**. No red run of unknown origin to file.

## Extra finding (observed, not filed here)

`:9391/health.data_cache` reports `reload_errors=134`, last `load failed: JSONDecodeError:
Unterminated string starting at: line 26030 column 14 (char 786425)` against `registry.json`, and
`chains_snapshot.latest=null / count=0`. A repeatedly failing registry reload is a data-plane
health gap (TR-374 territory) — recorded here, no second id minted.

## Board bookkeeping

- `TR-REV2-20261027-02`: closed on the deploy evidence (both listeners `stale=false`,
  `loaded_commit == repo_commit`); the "`make restart-router` exits 0" arm is **not literally met**
  and is transferred to the new row.
- `TR-CONTRACT-20261106-01` (P1): the sidecar drift that still aborts step 2.

## Tip at close

Evidence commit is the judged tree; canonical `main` and `origin/main` at parity, `git rev-list
--count origin/main..HEAD` = 0.
