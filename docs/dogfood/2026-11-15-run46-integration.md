# Dogfood run 46 — task-router (2026-11-15, tick `task-router-dogfood-2026-11-15-15-59-32`)

Lane: `task-router-dogfood` (skill-driven, no driver script). Target `/home/kara/task-router` at
`326d36ec` (branch `main`, parity with `origin/main` 0/0, clean tree before the board append).
Host: 16 cores, loadavg `3.51 5.05 8.35`, root filesystem 93% used (135 G free).

This fire used the product the way two different consumers do: a **CLI user** in a hermetic
`git archive` of HEAD (install → seed → validate → spawn → decide → verify) and an **HTTP consumer**
against the live read-only router on `127.0.0.1:9092` (health → resolve → gaps → listings → decision
→ verify → OpenAPI). Perf was measured on the headline operations; the install leg was attempted on
the bunker estate.

## Verdict

**PROMISING-BUT-ROUGH.** Every modern surface works when you drive it: `router spawn` resolves a
head with a real price in 198 ms warm, `router decide` mints a signed receipt with binding and
tamper rejection, and the served `/resolve` answers 200 in ~300 ms. What a real consumer cannot do
is trust the documentation over the deployment: the module that documents the decision surface
advertises a read-only path that the server refuses, and the install leg is unavailable for reasons
outside the product.

## What actually happened (real use)

1. **Install (fresh tree).** `git archive HEAD | tar -x` into scratch, `python3 -m venv`, then
   `pip install -e .` plus `duckdb`. Installs clean; `router` is on the venv bin path.
2. **Seed.** `router seed` refuses without a flag (`SATISFIABILITY FAIL`, `math` domain —
   `ollama-cloud/qwen3.5:397b` disabled and unpriced). Known and owned by `TR-384` — not re-filed.
   With `--allow-unsatisfiable` it completes and writes `registry.json` (7,973,096 B).
   **The refusal path is clean:** in a byte-exact export it exits 1, writes **no** `registry.json`
   and leaves all 32 `data/tables/*.jsonl` byte-identical, so the premise of the completed
   `TR-1000976681059248` (refused seed still publishes a registry) no longer reproduces.
   **So does `TR-1000976681059257`:** a clean export seeded with `--allow-unsatisfiable` leaves
   **32/32 tracked tables identical**, and replaying `validate` → `status` → `spawn` → `decide` on
   top changes none of them either. The delta I did see lived only in the long-lived pass1 tree
   (`model_tier.jsonl` *and* `category_levels.jsonl`, after a bare `seed` preceded the flagged one)
   and is **unattributed** — recorded as `TR-1000976681059261` so a worker re-attributes before
   "fixing" a seed that is already clean.
3. **CLI use.** `router validate --json` agrees with the registry; `router status --format json`
   reports the live catalog. `router spawn task-router --format json` returns a head provider/model
   with `usd_1m`, a sorted chain and exclusions; `router spawn P1_CODING` resolves as a profile.
   An unknown project returns the documented fail-open JSON error and **exit 0** — that is the
   README's stated contract (line 7), so it is a contract, not a defect.
4. **Decision path.** `router decide --init-receipt-key`, then a spec-shaped request
   (`version 1`, `parent_run_id run-dogfood-1115`, `step-1`, `generation 0`,
   `required_levels.code_gen 3`, `required_context_limit 200000`) produces a signed receipt with
   `valid_until`, candidate/exclusion counts and a `receipt_digest`. `router decide --verify` accepts
   it under `--expect-parent-run/--expect-step/--expect-generation`, and the same receipt with
   `head.model` edited is rejected. Binding and tamper rejection both hold.
5. **HTTP use, read-only instance.** `GET /health`, `/resolve` (low and high), `/gaps`,
   `/listings/models`, `/openapi.json` all answer 200. `POST /api/v1/execution-decision` answers
   **403 `read-only mode`** and never reaches the resolver; `POST /api/v1/execution-decision/verify`
   answers without a key (correctly `ok=false` for an unbound receipt). The in-code reason is
   deliberate (`router_server.py:1368`, H1-F4: the decision seam appends an issuance record, so it is
   a write). The **docstring in the module that documents this surface still says the opposite** —
   that is the new finding of this run.

## Findings filed this tick

| id | what | evidence |
| --- | --- | --- |
| `TR-1000976681059260` | `scripts/router_decision.py:30` advertises `POST /api/v1/execution-decision (+ /verify), read-only mode OK`; a read-only instance refuses the decision POST **403 before auth** (`router_server.py:1109`, allow-list at `1374-1377` holds only `/verify`), so the module contract contradicts the server's deliberate write decision | live probe 16:32Z + code proof |
| `TR-1000976681059255-R3` | install leg never started: `bunker-mvp` 97% and `bunker-las-02` 100% both refused at the 94% disk admission gate; `bunker-las-01`, `bunker-las-04`, `cube-las-00` unreachable → **SKIPPED-install-bunker**, zero cells executed | `bunker-qa.sh status`/`launch` output 16:41–16:45Z |
| `TR-1000976681059261` | `TR-1000976681059257`'s premise (seed mutates the tracked `model_tier.jsonl`) **does not reproduce** at `326d36ec`: clean seed + `--allow-unsatisfiable` = 32/32 tables byte-identical, and the read commands change nothing; the pass1-only delta is unattributed | three-way replay 16:40–16:52Z |

Re-probed, still owned, **not** re-filed: `TR-384` (seed refusal), `TR-1000976681059253` (served
`/resolve` drops `effort`), `TR-1000976681059254` (served build older than HEAD),
`TR-1000976681059258` (`boardctl validate` red on a tree with pre-existing board damage),
`README-6` (fresh clone cannot seed). `TR-1000976681059248` is complete and its premise no longer
reproduces (a refused seed writes nothing) — noted on `TR-1000976681059261`, not re-opened.

## Install leg — explicit, not a silent pass

`bunker-qa.sh status`: `bunkerd=active docker=yes act=no uidmap=yes` on `bunker-mvp` and
`bunker-las-02`. `bunker-qa.sh launch /home/kara/task-router`:
`PREFLIGHT FAIL: target host bunker-mvp disk usage 97% exceeds 94% launch threshold`; the same launch
on `bunker-las-02`: `100%`. The remaining configured servers (`bunker-las-01`, `bunker-las-04`,
`cube-las-00`) probe `UNREACHABLE`. **No agent was spawned, no cell ran, no evidence file exists** —
the fresh-install cell is SKIPPED for this tick, recorded as `TR-1000976681059255-R3`. This is
upstream of the `-R2` collect defect and must not be read as that failure repeating.

## Performance (Step 2b)

| operation | command | number | notes |
| --- | --- | --- | --- |
| cold `router spawn` | `/usr/bin/time -f '%e %M' router spawn task-router --format json` | **0.19 s**, 49.8 MB RSS | first exec in a fresh venv |
| warm `router spawn` | `hyperfine --warmup 2 --runs 8 'router spawn task-router --format json'` | **197.9 ms ± 9.4 ms** (user 165.6, sys 32.3) | loadavg 3.61 |
| served `/resolve` (warm) | 6× `curl -w '%{time_total}' '…/resolve?project=task-router&effort=high'` | **300 / 299 / 317 / 296 / 291 / 300 ms** | one HTTP hop over the same resolver |
| served `/health` (warm) | 3× `curl` | 189 / 125 / 131 ms | server liveness |

Nothing here is slow enough for a user to feel, so **no PERF row is filed** — filing one would
devalue the real ones. The only observation worth carrying forward is the ~100 ms gap between the
served `/resolve` (~300 ms) and the CLI's in-process resolve (~198 ms), which is a server-side cost,
not a resolver cost; it is unprofiled because a quarter of a second is not discomfort.

## Left behind

- `docs/dogfood/2026-11-15-run46-integration.md` (this file)
- `docs/dogfood/2026-11-15-run46-diagnostics.md` (the command-by-command trail)
- a run-46 section in `skills/task-router-usage/SKILL.md`
- this tick's entry in `.coding-hermes/dogfood-log.md`
- two board rows (`TR-1000976681059260`, `TR-1000976681059255-R3`, `TR-1000976681059261`)

No product code, no existing board row's status, no visibility, permission, scheduler or credential
was touched. All probing ran in `~/.hermes/cache/scratch/dogfood-task-router-2026-11-15-15-59-32/`
(`pass1` installed tree, `pass2a` unseeded control, `pass2b` seed probe).
