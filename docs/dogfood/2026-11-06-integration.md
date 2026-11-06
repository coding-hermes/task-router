# Task-router integration report — 2026-11-06

Scheduler tick `task-router-dogfood-2026-11-06-12-08-29`. Target HEAD
`586dcf6b16bde9a3b722a65a805bf4e090819a52` (canonical checkout clean, at parity with `origin/main`).
Method, evidence paths and the profiler boundary: `2026-11-06-diagnostics.md`.

## Promise (what the docs claim a user gets)

Install from a fresh clone, `router seed` the compiled registry from the committed tables, then
resolve any task profile into a gated, price-ordered provider/model chain with the full explanation
of exclusions, fail-open (exit 0 + JSON error) when resolution fails, and `router validate` as the
one-command integrity verdict. `make seed && make validate` is documented as passing "with no env
vars at all" (README.md:54).

## Reality — the path a real user walks

Fresh machine, Python 3.14.9, isolated venv: venv 2.63 s, `pip install -e .` 5.72 s,
`pip install duckdb` 7.48 s. Pre-seed behaviour is exactly as documented: `spawn P1_CODING` resolves
11 hops from `data/tables` with `fallback_used: true`, `validate` exits 1 naming `registry.exists`.

Then the quick-start step 4 fails:

```
$ router seed
SATISFIABILITY FAIL: 6 of 264 rating(s) in the emit domain have NO routable lane, across 1 category
  math: levels 0..5 — no lane MEASURES this capability ... blocked holder(s): ollama-cloud/qwen3.5:397b tier=5 (disabled, unpriced)

Seeding REFUSED: add a routable lane ... or re-run with --allow-unsatisfiable
```

…and it is **not** a refusal. The same run leaves a 6.0 MB `registry.json` (validate then says
`{"valid": true}`, 14/14) and rewrites two tracked files. On the clean git clone (no env vars):

```
$ make seed          # Makefile:59-60, no flags
make: *** [Makefile:60: seed] Error 1        rc=2, 50.4 s
$ git status --porcelain
 M data/tables/category_levels.jsonl
 M data/tables/model_tier.jsonl
$ make validate
{"valid": true, ... 14/14 ...}               rc=0, 3.0 s
```

`model_tier.jsonl` carries 11 **semantic** changes (MiniMax-M2.7 `mock` tier −3 → −2); the rest of
`category_levels.jsonl` is float-repr noise. So the documented compound command fails on its first
half and goes green on its second, against an artifact the refusal produced — and a first-time user's
next `git add -A` silently lands an unreviewed routing-data rewrite.

With the tool's own documented opt-out (`router seed --allow-unsatisfiable`, 27.9 s ± 1.0 over 3
runs) everything downstream is clean and correct: `validate --json` 14/14, `spawn P1_CODING`
(`source: registry.json`, `fallback_used: false`, 11 hops, head `opencode-go/deepseek-flash` at
0.029538 plan-effective/task), ad-hoc `--profile-req 'reasoning=5 debug=3 min_context=100000'`
(90 hops, 5 exclusions), `estimate` (690 ms), `spawn definitely-not-a-project` → rc 0 + JSON error.

## The strongest surface works: route receipts

`router decide` (ACP-ROUTER-001) is the contract a consuming foreman actually uses, and it holds
end-to-end once the operator key exists (`--init-receipt-key`, done inside an isolated
`TASK_ROUTER_HOME` so no host state was touched): `eligible: true`, 42 ordered candidates, a
900 s `valid_until`, and `--verify` → `ok: true`, `signature_ok: true`, `issued: true`.
Editing one authority-bearing field in the receipt (`head.model`) flips verification to rc 1 with
`DIGEST_MISMATCH` and `RECEIPT_SIGNATURE_INVALID`. The 503/`RECEIPT_KEY_UNAVAILABLE` path before
bootstrap is well-worded and documented (README.md:233), and the request validator rejects an
unknown category by printing the closed vocabulary — good error design, no row filed.

The read-only server (`--mode read-only --port 19192`) behaves as advertised: `/health` 200 (90 ms),
`/openapi.json` 200 (2 ms, 36 paths), `/metrics` 200 (248 KB), `/resolve` 200 (451 ms),
`/api/ui/chain` 200 (482 ms); `POST /api/v1/execution-decision` → 403 and `POST /ledger/start` → 403
with the `--mode read-only` explanation. `POST /api/v1/outcomes` appends in read-only mode **by
design** (`docs/integration.md:195`) — observed, documented, not filed.

## Findings

| # | Finding | Row |
|---|---|---|
| 1 | `router seed` prints `Seeding REFUSED`/exit 1 but still writes `registry.json` and rewrites 2 tracked `data/tables` files (11 semantic tier edits), so README.md:54's `make seed && make validate` claim fails on the seed half while `validate` goes green on what the refusal wrote | **filed: `TR-1000976681059248`** (P1) |
| 2 | Flagless seed refuses because `math` has no routable lane (`ollama-cloud/qwen3.5:397b` disabled + unpriced) — rc 1 flagless, rc 0 with `--allow-unsatisfiable` | reproduced at `586dcf6`; pre-existing `TR-1000976681059246`, no sibling filed |
| 3 | `scripts/router_mirror_account.py --commit` dies `NameError: name 'os' is not defined` at `:71` (no `import os`), rc 1, `models.jsonl` unchanged; `--dry-run` works | reproduced live; pre-existing `QA-TASKROUTER-13`, no sibling filed |
| 4 | Lint cell reports N/A while `pyproject.toml` declares `[tool.ruff]` + a pinned ruleset and the board venv ships ruff 0.15.22 | re-proved this run (cells `lint N/A` + config) ; pre-existing `QA-TASKROUTER-8` |
| 5 | Fresh-machine suite is red on the interpreter a supported Debian ships: `tests/test_metrics_endpoint.py:350` asserts `_peer_is_loopback("::ffff:127.0.0.1") is True` — False on python 3.12.3, True on 3.14.9. The function (`router_server.py:5431-5444`) is the TR-377 loopback gate behind `/metrics`, so a widened bind (TR-378) would 403 an IPv4-mapped loopback client | **filed: `QA-TASKROUTER-14`** (P2) |
| 6 | `bunker-qa.sh collect` declares a live agent gone (`AGENT-GONE … ssh probe returned no state`) while `bunker info` says running and `bunker exec` works | reproduced this run; pre-existing `QA-TASKROUTER-12`, no sibling filed |

## Performance

Nothing a user would notice: spawn 317 ms, status 324 ms, estimate 690 ms, decide 627 ms, HTTP
`/resolve` 451 ms, `/health` 90 ms (all measured under loadavg 36.8 on 16 cores). The single felt
wait is `seed` at 27.9 s — a one-shot admin/quick-start step whose input scale has not moved since
the 2026-10-26 tick, and whose attribution arm was harness-blocked. **No PERF row filed**; a
number nobody can act on would devalue the real finding.

## Fresh-machine install leg

`bunker-mvp` was live (`/health` 404 = live bunkerd; `bunker-qa.sh status` → `bunkerd=active
docker=yes act=no uidmap=yes`; client `bunker 0.2.0`) and the battery ran detached as agent
`301926f4` with evidence `/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl`.

Cells recorded by the collector: `launch OK`, five `toolchain-bootstrap OK` rows (go 1.27.3 into
`~/tools/go`, python venv + pytest, gcc/g++ wrappers, make, jq), `fresh-install OK`
(`Successfully installed duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0`), `lint N/A` (detector: "no
supported lint trigger" — see finding 4).

Battery outcome and teardown (final): cells recorded were `launch OK`; five `toolchain-bootstrap OK`
rows (go 1.27.3 into `~/tools/go`, python venv + pytest on the box's python **3.12.3**, gcc/g++
wrappers, make, jq); `fresh-install OK` (`Successfully installed duckdb-2.0.0 pyyaml-6.0.3
task-router-0.2.0`) — installability proven on a clean machine; `lint N/A` (detector: "no supported
lint trigger" — finding 4); `tests FAIL` at `tests/test_metrics_endpoint.py:350` (finding 5);
`docker_build N/A` and `compose_config N/A` (no triggers). The `act`/docker `ci` cell also failed
(31m28s, "Job 'test' failed", no assertion in its tail) — not attributed, so no claim is made about
it.

The collector could not finish on its own: `bunker-qa.sh collect` reported
`AGENT-GONE ... cannot reach agent=301926f4 (ssh probe returned no state)` **while the agent was
alive** — `bunker info` said `Status: running` and `bunker exec` answered `ALIVE`. That is
`QA-TASKROUTER-12` reproduced, and it is why the last cells were read by hand from
`~/qa-evidence.jsonl`. Teardown: `bunker destroy` rc 0 (home 7.0 GB archived), local key removed,
fresh `bunker list --server bunker-mvp --status all` → "No agents found", `bunker info` →
`not_found`. No residue, no skipped cell passed off as green.

## Verdict

🟡 **PROMISING-BUT-ROUGH.** The routing engine and the receipt contract are real and correct: a
stranger can install, resolve a price-ordered chain, price it, and get a tamper-evident decision
receipt, all in under 16 s of setup. What is rough is the seam the documentation points a new user
at first — the seed step refuses in words while writing state in fact, and the one-command verify
path then disagrees with itself (seed red, validate green). That single defect makes the first five
minutes untrustworthy for anyone who has not read `router_seed.py`.

Time to first success on the documented path: ~16 s to a pre-seed chain, blocked at step 4 for a
seed claim; with the documented opt-out, ~44 s to a seeded, validate-green registry.
