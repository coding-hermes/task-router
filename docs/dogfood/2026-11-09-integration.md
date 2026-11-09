# Task-router integration report — 2026-11-09 (run 42)

Scheduler tick `task-router-dogfood-2026-11-09-12-54-05`. Product under test:
`/home/kara/task-router` at `d91301167dce5977842ed604d64e1d1b95029663` (the binary every
command below ran is the editable install `~/.hermes/venvs/board/bin/router`, which resolves to
that tree). `HEAD` at report time is `8721ec08` and the diff `d9130116..8721ec08` over
`task_router tests scripts data Makefile pyproject.toml README.md` is **empty**, so the numbers
apply to the current tip. Exact commands, captures and the blocked-command trail:
`2026-11-09-diagnostics.md`.

## Promise (what a user is told they get)

Install from a clone, seed the registry from the committed tables, then ask the router which
provider/model should run a task: `router spawn <profile|project>` returns a price-ordered,
gated chain with the head pair, an explanation of every exclusion, and `fallback_used` when the
registry is short. `router validate` is the one-command integrity verdict. For machine callers
there is an HTTP surface: `router server --mode read-only|edit` with `/api/v1/execution-decision`
and a signed receipt that `/api/v1/execution-decision/verify` checks. `router gaps` is the
data-quality audit — the command that tells you which active models still lack price, catalog,
benchmark, perf, sentiment or notes.

## Reality — the path an agent actually walks

Everything a user does in the first five minutes works, and works fast:

- `router seed` still refuses on a flagless run (`SATISFIABILITY FAIL: 6 of 264 rating(s) ... math:
  levels 0..5 — no lane MEASURES this capability` → `Seeding REFUSED`, rc 1). Re-running with
  `--allow-unsatisfiable` seeds `registry.json` (6.98 MB) and syncs `data/tables`.
- `router validate` → `valid: all 14 checks passed`.
- `router spawn P1_CODING --format json` → head `xkiro / openai/gpt-6-luna`, 24-hop chain,
  `fallback_used: false`; `router spawn my-project` and `router spawn no-such-project-xyz` both
  answer with a structured result rather than a traceback.
- `router decide --request …` mints a signed receipt; `router decide --verify` returns
  `signature_ok: true` for it.
- HTTP: read-only server answers the documented GET surface and `POST /api/v1/execution-decision`
  + `/verify` (verify of a good receipt: `200` in 2.1 ms, `{"ok":true,"problems":[]}`).

Two things a real user would not expect:

**1. `router gaps` gives a clean bill of health on zero data (filed as `QA-TASKROUTER-22`, P2).**
Same binary, same shell, same machine — only the working directory differs:

```
$ cd ~/.hermes/scripts && router gaps ; echo rc=$?
== registry data-quality gaps (0 of 0 active models) ==
no gaps - every active model has price, catalog, benchmark, perf, full tiers, sentiment evidence and notes
rc=0

$ cd /tmp && router gaps ; echo rc=$?
== registry data-quality gaps (790 of 790 active models) ==
aws-bedrock/amazon.nova-2-lite-v1:0  price=$0.3300  missing(6): catalog, benchmark, perf, tiers(1/24), sentiment, notes
...
rc=0
```

`task_router/cli.py:147` resolves the repo root by walking cwd ancestors and taking the first one
that contains `scripts/router_spawn.py` (cli.py:168-181); the canonical install dir `~/.hermes`
has exactly that shape, so `_data_dir()` (cli.py:245) points at a `data/tables` that does not
exist and `scripts/router_gaps.py` renders "0 of 0" as success. The audit surface lies in the
reassuring direction — a user standing where the scripts live is told to stop looking.

**2. The registry is seeded but the shipped suite is red on the clean box (existing row
`QA-TASKROUTER-14`, reproduced, not re-filed).** Fresh install leg on bunker agent `b54d42ff`
(Python 3.12.3): `pip install -e .[dev]` → `duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0`,
artifact `~/tools/venv/bin/router` — install is genuinely green. Tests: `1 failed, 856 passed,
10 skipped in 731.79s`, the single failure being `test_peer_is_loopback_fails_closed`
(`::ffff:127.0.0.1` is not `is_loopback` on CPython 3.12.3).

## Non-findings (checked because the earlier log listed them as open)

- **Receipt verification fails closed.** Valid receipt → rc 0, `signature_ok: true`. Tampered
  receipt (`head.model` → `openai/gpt-6-sol`) → rc 1, `signature_ok: false`. Wrong
  `--expect-parent-run` → rc 1. An `rc=0` reading of the tampered case in an earlier pass was a
  shell artifact (`| jq` swallowing the status), not product behaviour.
- **TR-1000976681059248 (refusal writes `registry.json` anyway) is fixed.** Refusal on a fresh
  `TASK_ROUTER_HOME` exits 1 and leaves the home directory empty — no 7 MB artifact behind.
- **Edit-mode HTTP auth is enforced and does not leak.** `/api/v1/execution-decision` on
  `--mode edit` → `401` without a key, `401` with a wrong key, `200` with the right key
  (`{"receipt_id":null,"error":"unauthorized"}` for the refusals); keyed/unkeyed requests
  alternated `200`/`401` across two rounds.

## Performance (Step 2b)

Nothing on the user-facing path is slow enough to notice, so no PERF row is filed:

| operation | command | time |
| --- | --- | --- |
| resolve a chain | `router spawn P1_CODING --format json` | 0.232 s real (user 0.192 / sys 0.039) |
| integrity verdict | `router validate` | 0.124 s real (user 0.094 / sys 0.029) |
| HTTP verify | `POST /api/v1/execution-decision/verify` | 2.1 ms server-side |

The only discomfort measured is the full suite: **731.79 s on a 2-vCPU clean box** (856 tests,
≈0.85 s/test, subprocess-heavy CLI tests). That is contributor friction on a small machine, not
a user-visible product cost, and there is no profile identifying a hot path — reported here
rather than dressed up as a PERF finding.

## Install leg

Run through `bunker-qa.sh` against `bunker-mvp`: `status` → `bunkerd=active docker=yes act=no
uidmap=yes`; the cell evidence for agent `b54d42ff` shows `fresh-install OK` and `tests FAIL`
(details in the diagnostics report). Not a silent pass, not a skip.

## Verdict

**Works and is worth using; one trust defect.** The router does what it promises at a speed no
user can feel, the receipt chain is tamper-evident and fails closed, and a fresh machine can
install it. The defect is the audit surface: `router gaps` renders "no gaps" when it in fact read
no data, and where it does that (the install dir) is the directory a user is most likely to run
it from. Filed as `QA-TASKROUTER-22` (P2) with four acceptance criteria, including the missing
zero-data test.
