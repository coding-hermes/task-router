# The quality ladder — one data-driven score, and the rule that it never blocks

## What this is

`data/tables/quality_ladder.jsonl` is the single data-driven ladder for the
repo's quality scores: one JSON row per (metric, stage), each carrying the
metric, the stage (0–5), the target (a numeric percent), the priority, the
`block` flag, the `definition_cmd` that mechanically re-derives the number,
and a note. `scripts/quality_score.py` is the score verb that reads that
table and prints every metric's current value, the stage it has reached, and
the next target.

Three metrics are in scope:

| metric | what it measures | status |
|---|---|---|
| `type_hint_pct` | functions in `scripts/` with a return annotation / all functions (arg coverage printed alongside as a separate number) | **instrumented** — AST scan; measured 2026-10-03: 1.96% (22/1120) |
| `coverage_pct` | test coverage of the suite | **not instrumented** — TR-187 pending; targets seeded at 0 until then |
| `wiring_pct` | features implemented and tested but never wired (importers of the flag, selector reads) | **not instrumented** — TR-282 pending; targets seeded at 0 until then |

An unmeasurable metric is never reported as a silent 0: `quality_score.py`
prints a named `METRIC-UNMEASURABLE` line and exits nonzero when the metric
was the explicit ask. A fabricated 0 would look like a measured baseline and
poison the floor logic below.

## Stage → priority mapping

Stage 0 = P0, stage 1 = P1, stage 2 = P2, stages 3, 4 and 5 = P3. The
type-hint targets (owner directive 2026-10-03):

| stage | priority | target |
|---|---|---|
| 0 | P0 | 20% |
| 1 | P1 | 40% |
| 2 | P2 | 60% |
| 3 | P3 | 70% |
| 4 | P3 | 80% |
| 5 | P3 | 90% |

`coverage_pct` and `wiring_pct` carry the same stage/priority spine with
targets seeded at **0** — the defensible seed while no runner exists is 0,
not a guess; when TR-187/TR-282 land their first measurement, re-seed the
rungs from the measured baseline (edit the JSONL rows, not the script).

## Definition commands

Every stage row's `definition_cmd` is the same verb, pinned to the metric:

```bash
python3 scripts/quality_score.py --json --metric type_hint_pct
python3 scripts/quality_score.py --json --metric coverage_pct   # METRIC-UNMEASURABLE until TR-187
python3 scripts/quality_score.py --json --metric wiring_pct     # METRIC-UNMEASURABLE until TR-282
```

The whole score sheet, human-readable:

```bash
python3 scripts/quality_score.py
```

`type_hint_pct` is measured by a **Python AST scan** over `scripts/` (the
method is stated in the output): every `FunctionDef`/`AsyncFunctionDef`
counts, return-annotated over total is the headline number, annotated
params over all params is printed separately. Argument coverage is a
different number and is never merged into the headline.

## THE RULE: stage targets never block

**A stage target never blocks a commit, a CI run, or a tick close.** As
other work comes down, the ladder is not important — it never gets to be
the blocking reason. A low absolute score is information, not a gate; the
ladder exists so the scores are measured and visible, not so they can stop
work.

The **only** blocking value is the **regression floor**: once a metric has
achieved a stage target, it must never drop below that target again. The
floor is computed, not stored — the floor row in the JSONL carries
`target: null` on purpose. `quality_score.py --check-floor` derives it from
the metric's last recorded baseline:

```bash
python3 scripts/quality_score.py --check-floor --baseline type_hint_pct=21.3
```

The floor is the highest stage target the **baseline** achieved (a 21.3%
baseline has achieved stage 0, so the floor is 20%). With no recorded
baseline — or a baseline below the first target — nothing has been achieved,
the floor is 0.0, and nothing can be blocked. The floor protects only what
was actually earned.

## Decisions inside the shape, stated so they are not read as oversights

| Decision | Why |
|---|---|
| Stage rows carry `block: false`; exactly one row kind (`metric: floor`) carries `block: true` | The owner rule is enforced in the DATA and pinned by `tests/test_quality_ladder.py` — any future `block: true` on a stage row fails the suite before it can wedge a tick. |
| Targets seeded at 0 for uninstrumented metrics, with the reason in `note` | A NULL/gap must carry a reason; 0 says "nothing promised yet", not "nothing measured". |
| `METRIC-UNMEASURABLE` + nonzero exit for uninstrumented asks | A silent 0 would be indistinguishable from a real measured baseline. |
| Floor derived from a caller-supplied baseline, never stored in the table | A stored floor goes stale the first time a target moves; derived-from-baseline re-derives itself. |
| Floor rows have no `definition_cmd` | The floor is computed by `quality_score.py` from the stage targets and the baseline — there is no separate command to re-derive it. |
