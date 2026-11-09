# Diagnostics trail — task-router dogfood 2026-11-09-12-54-05

Method and evidence for `2026-11-09-integration.md`. Run root:
`~/.hermes/cache/scratch/tr-df-20261109-1254-r1/` (call it `$S` below).

## What was tested, from where

- Executable: `~/.hermes/venvs/board/bin/router` — editable install whose finder points at
  `/home/kara/task-router/task_router`, i.e. the live working tree, not the clone.
  `$S/repo` holds a `git clone --no-hardlinks` checkout pinned at `d9130116` used for reading
  source; all commands ran against the live tree because that is what a user on this host gets.
- Isolation for state: `TASK_ROUTER_HOME=$S/home`, `ROUTER_STATE_DIR=$S/state`. The repo itself
  was left alone (`git status` after every step; the only tracked file that moved inside the repo
  was `.gitreins/tasks.yaml`, created 13:00:00 UTC by a gitreins task start belonging to another
  lane — not this tick, not touched).

## Commands and captures

| step | command | capture |
| --- | --- | --- |
| seed (refused) | `router seed` | `log-seed.txt` (`SATISFIABILITY FAIL` @ line 65, `Seeding REFUSED` @ 68) |
| refusal writes nothing | `TASK_ROUTER_HOME=$S/home-refuse-probe router seed` → rc 1, dir empty | `seed-refuse-probe.txt` |
| seed (forced) | `router seed --allow-unsatisfiable` | `log-seed-unsat.txt` (`registry.json` 6989.0 KB, `synced data/tables`) |
| validate | `router validate` | `log-validate.txt` (`valid: all 14 checks passed`) |
| spawn by profile | `router spawn P1_CODING --format json` | `log-spawn-p1.json` (head `xkiro/openai/gpt-6-luna`, 24 hops, `fallback_used:false`) |
| spawn by project | `router spawn my-project --format json` | `log-spawn-proj.json` |
| spawn unknown | `router spawn no-such-project-xyz --format json` | `log-spawn-unknown.json` |
| estimate | `router estimate P1_CODING` | `log-estimate.txt` |
| decide | `router decide --init-receipt-key`, `router decide --request request1.json --format json` | `log-decide1.json`, `receipt1.json` |
| verify good | `router decide --verify receipt1.json --expect-parent-run df-run-20261109-1254 --expect-step step-1` | `v-good.txt` (rc 0, `signature_ok:true`) |
| verify tampered | same, `receipt1-tampered.json` (`head.model`→`openai/gpt-6-sol`) | `v-tamper.txt` (rc 1, `signature_ok:false`) |
| verify wrong parent | `--expect-parent-run df-run-WRONG` | `v-wrongparent.txt` (rc 1) |
| status | `router status --format json` | `log-status.json` |
| gaps — arm A | `cd ~/.hermes/scripts && router gaps` | `gaps-arm-home.txt` (`0 of 0`, `no gaps`, rc 0) |
| gaps — arm B | `cd /tmp && router gaps` | `gaps-arm-tmp.txt` (`790 of 790`, gap lines, rc 0) |
| HTTP read-only | `router server --mode read-only --port 9791 --host 127.0.0.1` | `log-server.txt`, `http-*.json` |
| HTTP edit | `router server --mode edit --port 9792 --host 127.0.0.1` + `ROUTER_EDIT_API_KEY` | `log-server-edit.txt` |

Both servers bound to `127.0.0.1` only and were started with the scratch home/state, so no live
registry or gitreins state was reachable from them.

## Reproducing the P2 gap (`QA-TASKROUTER-22`)

Three-arm census, one binary, one shell, env unset (`TASK_ROUTER_HOME`, `ROUTER_STATE_DIR`,
`ROUTING_DATA_DIR`, `TASK_ROUTER_REPO`):

1. cwd `~/.hermes/scripts` → `0 of 0 active models`, `no gaps`, rc 0.
2. cwd `/tmp` → `790 of 790`, per-model gap lines, rc 0.
3. Source: `task_router/cli.py:147-181` (`_resolve_repo_root`: explicit env → `_parents(cwd)` →
   package dir, shape test `scripts/router_spawn.py`), `cli.py:245` (`_data_dir()` =
   `<repo>/data/tables`), `cli.py:47` (`router_gaps.py` receives it as `ROUTING_DATA_DIR`).
   `~/.hermes` contains `scripts/router_spawn.py`, hence wins the shape test and supplies a
   `data/tables` that does not exist.

## Install leg evidence (bunker)

`cd ~/.hermes/scripts && BUNKER_QA_SERVER=bunker-mvp ./bunker-qa.sh status`
→ `bunkerd=active docker=yes act=no uidmap=yes`.

The ephemeral agent from the launch (`b54d42ff`, `100.115.67.116`) was still alive; its own
evidence was read directly over SSH because the collect path would not reach it:

```
$ ./bunker-qa.sh collect --evidence /tmp/bunker-qa-evidence-20261109T125928Z-2460139.jsonl
COLLECT-ADOPT reason=recent tick-death-orphan agent=b54d42ff new_tick_pid=2742865
```

Control arm with a fabricated path returns `COLLECT-FAIL no-launch-record`, i.e. the failure mode
above is specific to a live agent, not to a bad path. A later collect attempt over the live agent
aborted with `ssh probe returned no state` while `ssh -i ~/.config/bunker/keys/b54d42ff
bunker-b54d42ff@100.115.67.116` worked interactively from the same shell — the collect path's
agent probe is broken, not the agent. Hence the evidence below was harvested by hand from
`~/qa-logs-91723/` and `~/qa-evidence.jsonl` (local copy `$S/bunker-evidence.jsonl`, 15 lines).

Cell summary: `toolchain-bootstrap OK` ×10, `fresh-install OK`, `tests FAIL`, `lint N/A`,
`docker_build N/A`, `compose_config N/A`.

- fresh-install detail: `Successfully installed duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0`;
  artifact `/home/bunker-b54d42ff/tools/venv/bin/router`.
- tests detail: `1 failed, 856 passed, 10 skipped in 731.79s`; the failure is
  `tests/test_metrics_endpoint.py::test_peer_is_loopback_fails_closed`.

Loopback semantics were probed away from the host to make sure the failure is environmental
rather than a bad assertion: in `python:3.12.3-slim`, `ipaddress.ip_address("::ffff:127.0.0.1").is_loopback`
returns `False`, while `127.0.0.1` and `::1` return `True` (`probe_loopback.py`, `probe_gate.py`
with `-v $S/repo:/repo:ro`).

## Tooling pitfalls hit this tick (not product findings)

- `bunker-qa.sh` is not on `PATH`; it must be run from `~/.hermes/scripts`.
- The collect path cannot talk to a live agent (above) — the install leg is only provable by
  reading the agent's own evidence directory.
- Harness permission gates blocked two commands this tick — creating a fresh venv with
  `pip install -e .` on the host, and a 5× `router spawn` timing loop. Neither was retried or
  re-routed; the perf numbers reported come from single `time` invocations of the board venv
  binary, and the fresh-install proof comes from the bunker leg.

## Board write verification

- id minted by the minter: `python3 ~/.hermes/scripts/board_next_id.py .coding-hermes/board/tasks.jsonl QA-TASKROUTER` → `QA-TASKROUTER-22`.
- appended one row + one `task_created` event (`event id 1794082887258446142` = previous max + 1).
- `grep -c '"id": "QA-TASKROUTER-22"' .coding-hermes/board/tasks.jsonl` → `1`;
  `grep -c 'QA-TASKROUTER-22' events.jsonl` → `1`.
- `git diff --stat -- .coding-hermes/board/` → `2 files changed, 2 insertions(+)`.
- `boardctl -C /home/kara/task-router validate` → `448 error(s), 565 warning(s)`; the pre-existing
  debt is unchanged by this append: no error line mentions `QA-TASKROUTER-22`
  (`validate 2>&1 | grep -c QA-TASKROUTER-22` → `0`), and the event id is strictly greater than
  every id already in `events.jsonl`, so no new "ids must ascend" error is introduced.
