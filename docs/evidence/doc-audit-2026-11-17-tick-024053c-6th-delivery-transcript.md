# docs-audit transcript — tick task-router-docs-2026-11-17-02-40-53 (6th delivery)

Frozen probe transcript. Authored by the docs-audit lane; it is the `evidence=`
for the row filed from this delivery. Do not edit after filing — a corrected
census gets a separately named addendum.

## Scope and revisions

- Target: task-router (`git@github.com:coding-hermes/task-router.git`), default branch `main`.
- Audited / published tip: `f5e73d9ed950194109f1101968235e3781c9f5ae`
  (`board(task-router): file DOC-32..DOC-34 from the docs-audit tick's 5th delivery`).
- Tip advance since the 5th delivery: none. `git rev-parse origin/main` returned the
  same SHA before and after every probe below.
- Probe roots (both clean detached worktrees at the audited tip, never the canonical
  checkout, which is 115 commits behind and carries 8 foreign dirty paths):
  - `/home/kara/.hermes/stand-in/docs/task-router/wt-024053-d6` — seeded-path execution.
  - `/home/kara/.hermes/stand-in/docs/task-router/wt-024053-d6b` — fresh-clone (pre-seed) probes + source reads.
- Binaries: `router` built from each probe root (`uv pip install -e .`) into
  `d6venv` (with `duckdb 2.0.0`) and `d6venv-noduck` (without duckdb), python 3.12.13.
- Isolation: every invocation ran with `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
  `XDG_DATA_HOME`, `TASK_ROUTER_HOME` and `ROUTER_STATE_DIR` pointing inside the
  stand-in. The production state dir `/home/kara/.hermes/model-router` had mtime
  `2026-11-17 01:58:08.420653360 -0500` before the sweep and the identical mtime
  after it.
- Reader-facing corpus scanned for documented commands: `README.md`, `CONTRIBUTING.md`,
  `SECURITY.md`, `docs/*.md` minus `docs/evidence|docs/dogfood|docs/reviews` and the
  dated `chains-*` / `coverage-*` / `completeness-backlog-*` logs — 44 files, 34 lines
  whose first token is `router` (18 distinct verbs; 11 of the 34 are prose false
  positives such as "router returns a shaped error", each read and excluded).

## Probe 1 — help surface at the tip

`router --help` -> exit 0, 28 commands listed:

```
chain-run, circuit, clinepass, context-call, decide, diff, estimate, gaps, ingress,
learn, ledger, lifecycle, maintain, metrics, modelsdev, outcomes, plan-sweep,
pricing, pricing-audit, probe, probefix, quota, seed, server, spawn, status,
validate, web
```

Blank descriptions on exactly 6 of the 28: `chain-run`, `context-call`, `ingress`,
`lifecycle`, `outcomes`, `pricing-audit` (re-verifies the open row DOC-28).

`router <verb> --help` for all 28 verbs -> exit 0 for 26; `pricing-audit` exits 1
after printing its audit report, which `README.md:652` documents verbatim
("Takes no flags — `router pricing-audit --help` RUNS the audit; exit 1 = traps
found, 0 = clean"). Not a defect: the doc matches the behavior.

Sub-help probed for every subcommand a usage line actually declares
(circuit, ingress, ledger, learn, modelsdev, outcomes, quota, clinepass):

- `router circuit status --json` -> exit 0, `{"pairs": [], "provider_breakers": [], "provider_majority": [], "class_counts": {}}` — flag documented at README:116, exists.
- `router ingress poll --once --limit 5` — both flags present in `ingress poll --help`.
- `router ingress serve --port/--token-file/--require-sig` — all present.
- `router quota set --detected-at`, `router quota status [--json]`, `router quota clear --all` — all present (docs/quota-gate.md:104-105 accurate).
- `router metrics --top-providers/--top-models/--top-pairs/--since/--profile/--json` — all present.
- `router estimate --project` and the positional form — both present (README:118-120 accurate).

No documented flag in the reader-facing corpus is missing from the parser, and no
documented verb is absent from `router --help`.

## Probe 2 — the documented install/seed/verify path, executed

Seeded path (`d6venv`, isolated `TASK_ROUTER_HOME` + `ROUTER_STATE_DIR`):

| documented command | rc | result |
|---|---|---|
| `router seed` | 0 | wrote `<TASK_ROUTER_HOME>/registry.json` (9671.1 KB) |
| `router status` | 0 | `"source": "registry.json"`, `"fallback_used": false`, `"warning": null` — matches README:104-106 |
| `router spawn my-project --format json` | 0 | `"head": {` present (non-null) |
| `router spawn P1_CODING --format json` | 0 | profile-in-project-slot works as README:59-62 claims |
| `router spawn --list-profiles` | 0 | 11 profiles |
| `router spawn --profile-req 'reasoning=5 debug=3 min_context=100000'` | 0 | 52 KB chain |
| `router validate` | 0 | `valid: all 14 checks passed` |
| `router estimate --project ... / positional` | 0 / 0 | identical 33,451-byte output (README:118-120) |
| `router diff 2026-08-31 2026-09-01` | 0 | 9219 bytes; both snapshots exist |
| `router quota status --json` | 0 | `"present": true`, empty gate set |
| `router ingress endpoints` | 0 | 375 bytes |
| `router metrics --top-providers 10 --since 7d` | 0 | 274 bytes |

## Probe 3 — fresh clone, no duckdb (the finding)

`d6venv-noduck` (task-router installed, duckdb NOT installed), clean worktree
`wt-024053-d6b`, no `registry.json`:

```
$ router spawn my-project --format json      # rc=0
  "source": "data/tables", "fallback_used": true      <- README:99-102 accurate
$ router validate                            # rc=1
  [FAIL] registry.exists: missing: <wt>/registry.json — run scripts/router_seed.py once to generate it
  INVALID — 1 issue(s): ...                           <- README:107-109 accurate
$ router status                              # rc=0
  "source": "data/tables", "fallback_used": true,
  "warning": "registry.json missing or unreadable — reading committed data/tables fallback"   <- README:105-106 accurate
$ router seed                                # rc=1, STDOUT 0 bytes, STDERR 0 bytes
$ ls registry.json -> No such file or directory
```

`router seed` does fail loudly. It fails SILENTLY: exit 1 with zero bytes on both
streams, and `README.md:73-75` promises the opposite before quoting an exact string:

```
Skipping the `duckdb` step is not fatal: only `router seed` fails, and it fails
loudly (`router: dispatch failed: No module named 'duckdb'`) instead of writing
a partial registry; the test suite needs it too (see above).
```

Mechanism, read at the audited tip:

- `scripts/router_seed.py:26-31` raises a deliberate operator message:
  `sys.exit("router_seed.py requires the duckdb package. Install it with: pip install duckdb")`.
- `task_router/cli.py:596` converts a non-int `SystemExit` code to `rc=1`
  (`code = e.code if isinstance(e.code, int) else 0 if e.code is None else 1`) and
  the block at `:594-608` never prints `e.code`, so the string is discarded.
- The only message-emitting path is `task_router/cli.py:608-610`
  (`router: dispatch failed: {e}`), which is an `except Exception` arm — a
  `SystemExit` never reaches it. Hence the README's quoted text cannot be produced
  by any code path, and the operator who skips `pip install duckdb` gets no
  diagnosis at all.

Consequence for the docs: README:73-78 is wrong twice — the quoted message is not
what ships, and nothing is printed.

## Re-verification of open rows (no new rows filed for these)

- DOC-28 — `router --help`: 6 of 28 commands still carry a blank description (list above).
- DOC-32 — `/openapi.json` completeness claim: the audited tip differs from the
  measured parent `b8381ef9` only in board + evidence files
  (`git diff --name-only b8381ef9 f5e73d9e` -> `.coding-hermes/board/events.jsonl`,
  `.coding-hermes/board/tasks.jsonl`, `docs/evidence/doc-audit-...-5th-delivery-transcript.md`),
  so no server code changed under the measurement; premise unchanged.
- DOC-33 — stale muster operation counts still printed at `docs/muster-spec.md:15`
  (19), `:66` (30), `:95` (30), `:109` (30), `:122` (31), `:273` (30), `:274` (30),
  `:275` (30), `:279` (30), `:293` (19 / 30).
- DOC-34 — `docs/configuration.md` still lists `ROUTER_DEGRADED_WALL_S` at `:142`
  and `:144`, and `ROUTER_QUOTA_ORDER_GUARD` at `:149` and `:299`.
- DOC-25 — `router seed --help`: prints `usage: router_seed.py [-h]` plus the
  one-line description when a data home resolves (122 bytes, exit 0), i.e. still
  only `-h`; the TR-383 `--allow-unsatisfiable` flag remains invisible. Owned by DOC-25.

## Board state at the tip

`boardctl -C <worktree> validate`: `RESULT: FAIL (455 error(s), 605 warning(s))` —
identical error count to the 5th delivery's base; all warnings/errors are inherited
(event-id ordering and legacy-key noise, none naming a DOC row). No repair attempted.

## UNVERIFIED / residuals

1. No T5: no user-visible path beyond the local CLI was driven (no proxy routing
   decision, no live HTTP surface read in this delivery).
2. DOC-33's live operation count (36) was measured by the previous delivery against
   a read-only server; this delivery re-measured only the doc-side text and the
   unchanged-tip argument.
3. DuckBrain memory leg not written: the row's marker states
   `memory=none:this-sweep-made-no-memory-write`.
4. Canonical checkout `/home/kara/task-router` remains 115 behind / 7 ahead / dirty;
   its stranded rows (75c11adc, 3d4fae67) are superseded and were not touched.
5. `python3 -m pytest -q tests/` (README:70) was not run; the suite is not the
   subject of this sweep.
