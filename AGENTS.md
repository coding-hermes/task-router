# AGENTS.md — task-router

## What this is

The deterministic task router for the coding-hermes fleet (Bane + router ops,
2026-08-26). A task declares its capability profile (signed levels −5..+5 per
category); the registry finds every model that clears ALL requirements; the
chain = eligible models sorted by effective price; provider gates (quota /
health / circuit) filter at resolve time. The scheduler foreman consumes this
via `~/.hermes/scripts/router_spawn.py` (board tasks TASK-ROUTER-001/002 on the
coding-hermes-scheduler board).

## Repo layout

- `scripts/` — the runtime tools (canonical source; live installs live at
  `~/.hermes/scripts/` — keep them in sync, see TR-004).
- `docs/` — integration spec, seed-date chains, namespace anatomy.
- `.coding-hermes/board/` — JSONL workboard (canonical, git-tracked;
  `board.db`/`*.parquet` are untracked rebuildable caches).
- `tests/` — smoke suite (skips when duckdb unavailable).

## Commands

```bash
~/.hermes/venvs/board/bin/python3 scripts/router_spawn.py <project> --format json
~/.hermes/venvs/board/bin/python3 scripts/router_circuit.py status
~/.hermes/venvs/board/bin/python3 scripts/provider_health_probe.py   # manual calibration run
python3 -m pytest -q tests/                                          # smoke suite
./scripts/gitreins-guard-tests.sh                                    # guard test command
```

## Rules

- **JSONL tables are generated** — never hand-edit
  `~/duckbrain/namespaces/routing/tables/*.jsonl` or the task-router ns
  `tables/`. Rebuild via `scripts/router_seed.py`, export, then commit.
- **Do not touch scheduler Go code from this repo** — the scheduler foreman
  implements integration via its own board (TASK-ROUTER-001/002).
- **PAYG (deepseek) is a legitimate fallback hop** — subs first, PAYG where
  price ranks it; never force PAYG as the primary when a healthy sub exists.
- **Fail-open is sacred** — `router_spawn.py` must NEVER block the scheduler:
  any error → `{"error": ...}` + exit 0. Keep it that way.
- Board edits: read-modify-append JSONL, never rewrite the file wholesale.
- Commit style: Conventional Commits + `Co-authored-by: Alexis Okuwa <wojonstech@gmail.com>` trailer.

## DuckBrain

- Own namespace: `task-router` (`~/duckbrain/namespaces/task-router/`) —
  tables/, state/, chains/, docs/. Synced to Hetzner S3 (s3://duckbrain) every
  15 min (JSONL delta) + daily git-history backup.
- Data namespace: `routing` (`~/duckbrain/namespaces/routing/`) — registry
  tables + `scripts/router_seed.py`.
- Fleet namespace: `coding-hermes` — shared fleet memory; per-tick project
  status keys written by the foreman.

## Board

`.coding-hermes/board/` JSONL canonical (tasks.jsonl + events.jsonl
git-tracked). Fixtures: NEVER-DONE, E2E-001, GITREINS-JUDGE. Task IDs `TR-00N`.
