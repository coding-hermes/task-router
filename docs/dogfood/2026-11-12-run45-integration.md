# Dogfood run 45 — task-router (2026-11-12, tick `task-router-dogfood-2026-11-12-13-41-25`)

Lane: `task-router-dogfood` (skill-driven, no driver script). Target `/home/kara/task-router` at
`65fab925` (branch `main`, parity with `origin/main` 0/0, clean tree). Host loadavg `6.61 5.95 5.76`
on 16 cores — a busy but normal fleet box.

This fire is a **new use of the product surface**, not a records-only repeat: it walked the CLI
install/seed/spawn path in a hermetic `git archive` of HEAD, timed the headline operations, and
reproduced the CI failure locally.

## Verdict

**PROMISING-BUT-ROUGH.** The router works when used: spawn returns a head with a real price, a
served `/resolve` answers 200 in ~290 ms, and `router validate` agrees with the registry. But a user
cannot trust what they see, because **the same commit tests differently inside CI than outside it**,
and main is red right now.

## What actually happened (real use)

1. **Install (fresh tree).** `git archive HEAD | tar -x` into a scratch dir, then the documented
   seed. The seed **refuses without a flag**: `SATISFIABILITY FAIL` on the `math` domain
   (`ollama-cloud/qwen3.5:397b` disabled + unpriced). This is known and owned by `TR-384`, so it was
   not re-filed. With `--allow-unsatisfiable` it completes and writes `registry.json`.
2. **Use.** `router spawn P0_FORE --format json --quiet` and `P2_AGENTIC` return a head model, a
   provider key and a `usd_1m` price; `router validate --json` reports the registry valid.
   `GET :9092/resolve?project=task-router&effort=high` answers 200 (`effort.requested=null` on the
   served build — already owned by `TR-1000976681059254`).
3. **The trust problem.** At commit `65fab925` the two named CI tests fail
   (`test_golden_fixed_point_heads` at the fore and agentic profiles) while the same tests **pass
   locally**. Reproduced deterministically: in a `git archive` of HEAD the set is **5 passed**;
   running the exact CI seed step (`ROUTING_ALLOW_UNSATISFIABLE=1 python3 -m scripts.router_seed`,
   `ci.yml:110-113`) first and then the same tests gives the **same failures CI reports**. The seed
   appends to the **tracked** `data/tables/model_tier.jsonl` (844,422 → 863,968 bytes), which is why
   the corpus differs between a dev checkout and CI at the same SHA.
4. **Install leg (bunker).** `bunker-qa.sh launch` OK → agent `8ca717de`; toolchain-bootstrap OK x5;
   fresh-install OK (duckdb-2.0.0, pyyaml-6.0.3, task-router-0.2.0); lint N/A; then `collect` →
   `ADOPT tick-death-orphan` / agent-gone, so tests/UI/perf never ran. Second occurrence of the same
   death (first: `TR-1000976681059255`, agent `c688aa9c`). Recorded, never a silent pass.

## Performance (Step 2b) — measured, no finding

Host loadavg `6.61 5.95 5.76`, 16 cores.

| operation | command | result |
|---|---|---|
| cold CLI spawn | `/usr/bin/time .venv/bin/router spawn P0_FORE --format json --quiet` | `0.26 s` / `0.26 s` / `0.25 s`, 74 MB RSS |
| warm HTTP resolve | `curl -w '%{time_total}' http://127.0.0.1:9092/resolve?project=task-router&effort=high` | `0.273 0.297 0.298 0.302 0.297 0.282 s` (http 200 every time) |
| warm registry load | `/usr/bin/time .venv/bin/router validate --json` | `0.03 s` / `0.02 s`, 17 MB RSS |

**Nothing here is slow enough that a user would notice**, so no `PERF` row and no profile were filed
— a win nobody can feel is not a finding. The one number worth watching is that the served
`/resolve` costs ~10x the CLI's registry read (≈290 ms vs ≈25 ms); that is recorded here as an
observation only, because this fire did not profile it and will not claim a hot path it has not
measured.

## Findings filed

| row | what it is |
|---|---|
| `TR-234-R2` | Recurrence of `TR-234`: the two golden params still pin `gpt-6-luna` while the seeded corpus resolves `nemotron-3-nano-omni`; main red at `65fab925` (CI 43246718474, also 43238672272) |
| `TR-1000976681059257` | The seed appends to the **tracked** `data/tables/model_tier.jsonl`, so CI and a dev checkout test different corpora at the same commit |
| `TR-1000976681059255-R2` | Recurrence of `TR-1000976681059255`: the bunker install leg died after the fresh-install cell again (agent `8ca717de`), 2 of 2 attempts |
| `TR-1000976681059258` | `boardctl validate` is red on pristine `origin/main` (458 errors / 603 warnings) and the hook is `warn`-mode, so the duplicate-id guard cannot block anything |

Plus one `audit` event on `TR-403`: the row's premise is guarded by
`test_corpus_is_effort_honest_today`, and that assertion now **fails** precisely because the corpus
it guards gained measured `high` cells from the seed step — closing `TR-403` without touching the
assertion leaves main red.

Not filed, deliberately: the `--allow-unsatisfiable` refusal (`TR-384` owns it), the served
`effort.requested=null` (`TR-1000976681059254` owns it), and the `bogus --effort` fail-open
(judged acceptable in run 44 and still acceptable).

## Left behind

`docs/dogfood/2026-11-12-run45-integration.md` (this file),
`docs/dogfood/2026-11-12-run45-diagnostics.md`, a `run 45` section in
`.coding-hermes/dogfood-log.md`, a `Dogfood run 45` section in `skills/task-router-usage/SKILL.md`,
and the four board rows above with five `boardctl event` records. Product code, repo visibility,
permissions, scheduler state and credentials were **not** touched.
