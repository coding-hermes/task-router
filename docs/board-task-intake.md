# TR-298 — automatic board task intake

Owner goal (2026-10-03): "every time we have a test go in we pick a model."
Until TR-298 a newly submitted board task only got its model resolve when a
human ran `router_spawn.py --from-task <id>` by hand.

## Source-of-truth event

A new row appended to the canonical board —
`.coding-hermes/board/tasks.jsonl` — with `"status": "pending"`. The append
IS the "task submitted" event in this fleet: every writer (foremen, the
board appender) adds rows, nobody rewrites them in place, so tailing the
file observes every submission exactly once.

## What `scripts/board_task_intake.py` does

Per poll (default every 30s):

1. `stat` the board (mtime + size). Unchanged → nothing to do.
2. Read only the NEW bytes from the tracked offset. Bytes of a trailing
   line without a newline are carried over to the next poll, so a row
   written across two polls is reassembled, never dropped. A truncated or
   malformed line is skipped — never fatal.
3. For each NEW pending task id not in the seen state:
   - append a **claim** to `data/state/intake-seen.jsonl` (before spawning —
     a daemon crash mid-resolve can never cause a second resolve);
   - run the existing resolve as a subprocess:
     `python3 scripts/router_spawn.py --from-task <id> --board <board> --format json`
     (ratings from classifier/JEV scoring of the real task text; candidates
     from the registry with their measured-or-price basis — nothing
     synthesized, this is exactly the manual path);
   - append the result to `data/state/intake-resolves.jsonl`:
     `{id, ts, source, resolved_at, chain_head: {provider, model, usd_1m},
     complexity_source, returncode, resolve: <full spawn payload>, error?}`;
   - complete the claim in the seen state.
4. Fail-open: any error (spawn crash, timeout, garbage stdout, the spawn's
   own `{"error": ...}` payload) becomes a record with an `error` field in
   the resolves ledger and the loop keeps running. The daemon never exits
   on a bad poll.

## Idempotence

The seen state (`data/state/intake-seen.jsonl`, one `{id, ts, resolved_at,
source}` row per event, LAST row per id wins) is the idempotence key:

- a task id is resolved exactly once, ever — replays of the same id
  (duplicate rows, `--once` again, a daemon restart) resolve nothing;
- `--once` drains the CURRENT backlog and exits (testing/CI): a fresh state
  + `--once` resolves every pending id already on the board;
- a daemon (no `--once`) cold-booting with a fresh state **seeds** every id
  already on the board as seen WITHOUT resolving them — intake begins with
  genuinely new submissions;
- a dangling claim (daemon killed mid-resolve) is reconciled at the next
  boot into an `error` record ("resolve interrupted before completion") —
  visible, attributable, and still never resolved twice.

## Runtime state (gitignored)

`data/state/intake-seen.jsonl` and `data/state/intake-resolves.jsonl` are
runtime artifacts, gitignored like the outcomes store (TR-049). They are
the audit trail: every resolve attempt — including failures — lands there.

## Running it (NOT installed — documentation only, TR-298 constraint)

Manual / CI (drain the backlog once and exit):

```bash
~/.hermes/venvs/board/bin/python3 scripts/board_task_intake.py \
    --board .coding-hermes/board/tasks.jsonl \
    --state-dir data/state --once
```

As a daemon (poll loop, every 30s):

```bash
~/.hermes/venvs/board/bin/python3 scripts/board_task_intake.py \
    --board .coding-hermes/board/tasks.jsonl \
    --state-dir data/state --interval-s 30
```

Systemd **user** unit template (drop as
`~/.config/systemd/user/task-router-intake@.service` if a host ever wants
it as a service; the `%i` is the repo path):

```ini
[Unit]
Description=task-router automatic board task intake (%i)
After=network-online.target

[Service]
ExecStart=%h/.hermes/venvs/board/bin/python3 %i/scripts/board_task_intake.py \
    --board %i/.coding-hermes/board/tasks.jsonl \
    --state-dir %i/data/state --interval-s 30
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Nothing is installed or started by this repo — enabling remains an explicit
operator decision (`systemctl --user enable --now
task-router-intake@/home/kara/task-router` on a host that wants it).

## Environment overrides

| Variable | Effect |
|---|---|
| `ROUTER_INTAKE_BOARD` | board tasks.jsonl path (same as `--board`) |
| `ROUTER_INTAKE_STATE_DIR` | state dir for the two ledgers (same as `--state-dir`) |
| `ROUTER_INTAKE_INTERVAL_S` | poll interval seconds (same as `--interval-s`) |
| `ROUTER_INTAKE_SPAWN_CMD` | base resolve command to extend per task (default: this python + `scripts/router_spawn.py`) |

## Verification

```bash
python3 -m pytest -q tests/test_board_task_intake.py
```

21 tests: exactly-once trigger, idempotent replay (duplicate row, second
`--once`, daemon restart, board rewrite), truncated + malformed lines,
fail-open subprocess errors (crash / timeout / garbage / spawn's own
error payload) with the loop continuing, dangling-claim reconciliation,
`--once` drain + idempotence, cold-boot seeding, spawn-argv shape against a
stub interpreter, and the gitignore/doc-parity guards.
