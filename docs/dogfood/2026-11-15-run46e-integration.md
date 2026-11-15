# run 46e integration — what this dogfood fire changes for the maintainer

Tick: `task-router-dogfood-2026-11-15-15-59-32` (fire 6). Tree: `origin/main` **5ce46384**.
Mode: skill-driven EXECUTOR (`coding-hermes-dogfood`), no driver script. Product code untouched.

## What the run proved

1. **It installs on a machine that never held it** — clone 4 s, editable install 5 s, `router --help` rc 0 on
   Python 3.14.9. No credential was needed and none was minted.
2. **It routes** — after seeding, `router spawn my-project` answers in under a second (warm 0.39–0.43 s) while
   parsing a 7.97 MB registry; that is the product doing its advertised job, not a test double.
3. **The honest first-run story is broken in two places**, and both are now rows instead of impressions.

## Rows filed

| id | priority | one line |
|---|---|---|
| `TR-1000976681059262` | P2 | `--format text` prints 5 of 71 chain hops with no marker and no flag to expand (`scripts/router_spawn.py:5643`) |
| `TR-1000976681059263` | P2 | README quickstart step 46 (`router seed`) exits 1 on a fresh clone of main; `--allow-unsatisfiable` documented only in the usage skill |

Both carry exact commands, the numbers, the `file:line`, and a pointer to
`.coding-hermes/evidence/DOGFOOD-2026-11-15-1859-taskrouter-46e.jsonl`.

**Not filed, deliberately:**

- **No `PERF-` row.** 0.39–0.49 s warm/cold with 75 MB RSS is not a discomfort a user reports. Filing it would
  devalue real ones. Baseline is in the diagnostics doc instead.
- **No recurrence row for the 455-error board lint.** `boardctl validate` fails identically on `HEAD` and on the
  working tree, so this fire did not cause it; `TR-1000976681059249` owns it and the count has drifted 415 → 455
  (recorded, not re-minted).
- **No fifth bunker row.** The install-leg recurrence chain already runs to `TR-1000976681059255-R4`, today's
  fresh-install cell is **OK**, and the tests FAIL is owned by `QA-TASKROUTER-14`. The only new fact is that no
  `.logs` were collected for the 18:22 battery, which is a note, not a row.

## What a maintainer should do next, in order

1. `TR-1000976681059262` first — it is two lines plus a test, and it changes what every user reads by default.
2. `TR-1000976681059263` next — either give `math` a routable lane on main (making the documented command true)
   or tell the truth in `README.md:42-46`. The current state teaches a new user that step 1 of the quickstart
   failing is normal.
3. Give the bunker battery a collector that survives the destroy trap, otherwise its `tests` verdicts stay
   unattributable and every dogfood fire re-reads the same FAIL.

## What this fire could not test (honest gaps)

- The bunker `tests` cell FAIL could not be attributed (no per-cell logs survived the 18:22 battery).
- Nothing past `spawn` was exercised — the tick path, the scheduler surface, and any long-running behaviour are
  outside this run's evidence.
- Whether a starved `math` lane is a data gap or a deliberate "knowingly-starved" posture is a product decision
  this run cannot make; the refusal text supports both readings.
