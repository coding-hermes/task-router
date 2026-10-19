# Dogfood run 36 — 2026-10-19 — the quality artifact: a producer with no consumers

Target: task-router HEAD `42e4e60` (control host) and the same commit freshly cloned inside an
ephemeral bunker agent. Angle: the TR-281 "score artifact" surface, landed `6a5d82e` (2026-10-18)
and closed with a Tier-2 PASS — never driven by runs 1–35.

Lane: `task-router-dogfood`, tick `2026-10-19-23-43-04`.

## Promise statement (the null hypothesis)

The project claims: **"one machine-readable quality artifact, one verb, one authority — so
gitreins, the board and the UI read the same numbers instead of each recomputing them."**
A user (or an agent asking "how good is this codebase right now?") should be able to read one
file and get the project's quality numbers, and every consumer should read that file.

## What was actually driven

| step | command | result |
|---|---|---|
| produce (warm) | `python3 scripts/quality_artifact.py --output /tmp/qa-dogfood-artifact.json` | rc=0, `schema_version=1`, `measured_at=2026-10-19T23:50:32+00:00`, artifact byte-identical to the committed one apart from the timestamp |
| the numbers | `jq` on the artifact | `type_hint_pct 52.2556` (138-module breakdown), `coverage_pct null` + reason `no coverage runner instrumented yet (TR-187 pending)`, `wiring_pct null` + reason `no wiring metric exists yet (TR-282 pending)` |
| the OTHER authority | `python3 scripts/quality_score.py --json --metric type_hint_pct` | `52.47`, `stage 1`, `next_target 60.0` |
| consumers | `git grep -n 'quality-artifact'` (tracked, minus `.gitreins/history`) | `scripts/quality_artifact.py`, `tests/test_quality_artifact.py`, `.gitreins/tasks.yaml` (the acceptance-criteria text), and the artifact itself. **No reader.** |
| guards/UI/CI/Makefile | `grep -rn quality .gitreins/config.yaml .github/workflows Makefile scripts/board_*.py scripts/router_server.py` | nothing that reads the artifact |
| fresh box | ephemeral bunker agent `df-tr-1019b` on `bunker-mvp` | clone `42e4e60` → verbatim `pip install -e .` rc=1 (PEP 668) → venv rescue 10s → `duckdb` 2s → `router seed` 31s → `router spawn P1_CODING` resolves → `quality_artifact.py` rc=0 in 1s, same value 52.2556 |

## Verdict

**🟡 PROMISING-BUT-ROUGH (surface-scoped).** The producer half is real, honest and fast; the
consumer half — the reason the row exists — was never built, and the row is closed as if it were.

- **Does it work?** Yes, as a producer: rc=0, 0.50s ± 0.03s warm (n=20, hyperfine), identical
  output on a clean machine, and it never fakes an unmeasurable metric (`null` + reason, never 0).
- **Is it useful?** Only if something reads it. Today nothing does, so the artifact is a dead
  file that satisfies a schema, not an authority — the exact "a number that only exists inside one
  tool is not awareness" problem the row was filed to fix, one level up.
- **Is it usable?** Not findable: no README, no `docs/`, no Makefile target mentions the verb.
  A user following the quickstart cannot learn the artifact exists.
- **Is it trustworthy?** The value is reproducible (identical on two machines, same commit) — but
  there are two computations of the same metric name (52.2556 vs 52.47), so "one authority" is
  not yet true even at the producer level.
- **Is it fast enough?** Yes, comfortably: 504.3ms ± 26.8ms warm (n=20) for the artifact verb,
  460.3ms ± 32.1ms for the ladder verb, both including a full AST scan of the tree; 1s cold on a
  fresh box; `router spawn my-project` 0.14–0.15s warm. **No PERF row filed** — nothing here is
  slow enough that a user would notice.

## Findings (filed on the board)

| id | prio | finding |
|---|---|---|
| `TR-1000976681059241` | P1 | TR-281 is closed but its consumer acceptance criterion is undelivered: zero non-test readers of `data/state/quality-artifact.json` (no gitreins GR-142/143 guard, no board script, no UI route, no CI step, no Makefile target). |
| `TR-1000976681059242` | P2 | Two producers, one metric name: artifact `type_hint_pct 52.2556` (`type_hint_score.py`, `scripts/` + top-level packages, 1596 functions) vs the ladder's own `definition_cmd` `quality_score.py --metric type_hint_pct` = `52.47` (returns only, `scripts/` only) — on the same tree, on both boxes. |
| `TR-1000976681059243` | P3 | The verb is undiscoverable: no README/docs/Makefile mention. |

Recurrence evidence (not a new row): lane-prompt misseed event refreshed as `20261019235704` —
`task-router-dogfood`'s stored prompt is still the **warpfs** dogfood brief verbatim (4th
confirmation; first recorded `20261013105203`). Target resolved from the **lane name** per the
dogfood skill, never from the prompt body.

## Install leg (fresh machine, ephemeral bunker)

- host `bunker-mvp` (the only reachable fleet bunker: las-01/02/03/04 offline), agent
  `df-tr-1019b`, destroyed after the run, **0 residue** after a 90s recheck, local key removed.
- documented path verbatim: `git clone https://github.com/coding-hermes/task-router.git` rc=0 in
  **2s**; `pip install -e .` **rc=1 — PEP 668 externally-managed-environment** (the README line
  69 documents the venv, so the rescue is documented-adjacent, not invented): `python3 -m venv` +
  `pip install -e .` **10s**, `pip install duckdb` **2s**, `router seed` **31s**, then
  `router spawn P1_CODING --format json` resolves (registry 1992 rows / 838 active / 29
  providers).
- time-to-first-success on the clean box ≈ **45s**; install leg **PASS** (nothing skipped).
- delta smoke on the same box: the TR-281 artifact verb (the surface under test) — rc=0, 1s cold,
  same 52.2556.

## Left behind

- `docs/dogfood/2026-10-19-run36-quality-artifact-consumers.md` (this report)
- `docs/dogfood/diagnostics.md` — run 36 section: how the artifact is built, why it disagrees
  with the ladder, how to re-prove both numbers
- `skills/task-router-usage/SKILL.md` — "Quality numbers (run 36)" section so the next agent
  knows both verbs, their scopes and the artifact contract
- `.coding-hermes/dogfood-log.md` — run 36 entry
- board rows `TR-1000976681059241..43` + events

## Not done / explicitly not claimed

- No code, no existing row and no scheduler state was modified (read-only lane).
- The foreman was **not** woken; the lane performs no scheduler mutations.
- The `:9092`/`:9093` UI was not started in this run (runs 31/34 covered the UI doors); the
  consumer grep is a static finding, not a live 404 claim.
