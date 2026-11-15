# run 46e diagnostics — 2026-11-15T18:59Z — fresh-clone real use

Tree: `origin/main` **5ce46384**. Venv: repo `.venv` for perf; fresh scratch clone for the install leg.
Isolated data homes only (`TASK_ROUTER_HOME` under `/home/kara/.hermes/cache/scratch/`), so no repo state,
registry, or scheduler row was exposed to the run.

## 1. Fresh-machine install leg

| step | command | result |
|---|---|---|
| clone | `git clone --quiet <origin> $D` | **4 s**, HEAD `5ce46384` |
| env | `uv venv .venv -q` | ok, Python 3.14.9 |
| install | `UV_LINK_MODE=copy uv pip install -e '.[dev]' -q` | **5 s**, rc 0 |
| help | `router --help` | rc 0 |

Documented quickstart, followed verbatim from `README.md:34-46` with a clean home:

| step | rc | note |
|---|---|---|
| `router seed` | **1** | `Seeding REFUSED: add a routable lane for the categories above, or re-run with --allow-unsatisfiable` — blocked capability **math levels 0..5**; only holder `ollama-cloud/qwen3.5:397b` tier 5, disabled + unpriced |
| `router validate` | **1** | `registry.exists` missing (downstream of the refusal) |
| `router seed --allow-unsatisfiable` | 0 | **17 s**, writes `registry.json` **7.97 MB** (100,905 `metrics.jsonl` lines) |
| `router validate` | 0 | < 1 s |
| `router spawn my-project` | 0 | < 1 s |

**Time-to-first-success on a fresh clone: ~27 s** (4 + 5 + 17 + <1 + <1) — *with* the flag. Without it the
documented first command exits 1 and there is no registry. `--allow-unsatisfiable` is documented in
`skills/task-router-usage/SKILL.md:43`, **not** in `README.md` (grep for `unsatisfiable` there: no hit).

## 2. Headline operation, timed

`router spawn my-project --format json --quiet`, repo venv, isolated home:

| run | wall | maxrss |
|---|---|---|
| cold | 0.49 s | — |
| warm 1 | 0.43 s | 75,336 kB |
| warm 2 | 0.41 s | 75,240 kB |
| warm 3 | 0.39 s | 75,048 kB |

Payload: **2,568 lines / 68,926 bytes**; `chain` length **71**, `exclusions` **14**. The 75 MB RSS is the cost of
parsing the 7.97 MB `registry.json` on every invocation — real, but 0.4 s is not a discomfort a user reports, so
**no `PERF-` row is filed**. Recorded here so a future regression has a baseline.

## 3. Text renderer vs JSON — the disagreement

`router spawn my-project --format text` on the same tree and home:

```
text output       : 21 lines / 3,236 bytes
hop lines         : 5   (positions 2..6)
EXCLUDED lines    : 14  (complete)
truncation marker : none  ("omitted", "truncated", "…", "of 71" all absent)
expand flag       : none  (no --all / --full / --limit in `router spawn --help`)
```

`chain` in JSON carries **71** hops. Cause is a hardcoded slice:

- `scripts/router_spawn.py:5643` — `for c in r.get('chain', [])[1:6]:`
- `scripts/router_spawn.py:5646` — exclusions printed in full (asymmetry: the rejected list is complete, the
  accepted list is not)

Reader consequence: the compact view says this project has five fallbacks. It has seventy-one. → row
`TR-1000976681059262`.

## 4. Install leg via the ephemeral bunker (cited, not re-run)

`bunker status --server bunker-mvp`: **ONLINE**, disk **83 % (125.1/149.9 GB)**, **3/50** agents — inside the
94 % admission gate. Today's cells already exist in `.coding-hermes/evidence/`:

- `DOGFOOD-2026-11-15-171454-bunker.jsonl`
- `DOGFOOD-2026-11-15-1822-bunker-battery.jsonl` — fresh-install **OK** (`duckdb 2.0.0`, `pyyaml 6.0.3`,
  `task-router 0.2.0`), tests **FAIL** (rc 1), act ci-pass killed by `BUNKER_QA_ACT_TIMEOUT 2400 s`.

Ownership already recorded: tests cell → `QA-TASKROUTER-14`; act budget → `QA-TASKROUTER-20-R2`; binder
recurrences → `TR-1000976681059255` (`-R2`, `-R3`, `-R4`). **No `.logs` directory was collected** for the 18:22
battery, so the FAIL cannot be attributed past its verdict; that gap is noted here rather than filed as a fifth
sibling row.

## 5. Board-write verification

- `boardctl create` → `created task TR-1000976681059262` / `TR-1000976681059263`
- `grep -c '"id": "<id>"' .coding-hermes/board/tasks.jsonl` → **1** and **1**
- events appended `…052 → …053 → …054`: ascending and paired with their rows
- `boardctl validate` → **`RESULT: FAIL (455 error(s), 590 warning(s))`** on the working tree **and identically
  on a pristine `git archive HEAD` copy** → my two rows add zero errors; `0` error lines name either row.
  Pre-existing class, owned by `TR-1000976681059249` (415 errors when filed, **455** now) and
  `TR-1000976681059258`.

## 6. Not established by this run

- Whether the bunker `tests` FAIL is environmental, budget-induced, or a genuine red — no per-cell logs survived.
- Whether `--allow-unsatisfiable` is the *intended* first-run path or a workaround for a starved `math` lane;
  the README does not say, and the refusal text alone cannot settle it.
- Anything past `spawn`: no execution/tick path was exercised, so latency here says nothing about routing or
  the scheduler surface.
