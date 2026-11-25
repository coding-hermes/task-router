# P0_FORE capability dispositions — long_horizon declared, tool_use unrequired (CF-246)

Dated 2026-11-26 (code-factory CF-246). How the foreman profile accounts for
the two capability categories this row adjudicates: one declared with a
satisfiable level, one documented as intentionally unrequired under an owner
regime lock.

## long_horizon — declared, level 2

`data/tables/task_profile_requirements.jsonl` declares:

```json
{"task_id": "P0_FORE", "category": "long_horizon", "level": 2}
```

Why: the live evidence (code-factory tick 2026-11-05, scheduler.db) shows the
inferred-matrix clamp wanted `long_horizon=2` and the tick hard-failed with
`requirements_unmet` because P0_FORE never declared the category. The
requirement is satisfiable today: the routable `long_horizon` rungs are
`[-5, -4, -3, -2, 5]`, so the best routable tier (5) clears the required
level (2). Verified against the committed tables in
`docs/audits/cf-246-satisfiability.txt`.

## tool_use — intentionally unrequired (owner regime lock)

P0_FORE deliberately does NOT declare `tool_use`. Reason: owner regime lock,
Bane 2026-08-27 (2nd correction) — tool-calling is table stakes in 2026, and
the foreman profile must not gate on a cliff-artifact category. The lock is
enforced by `tests/test_regression.py::test_p0_fore_has_no_tool_use_bar`,
which fails if a re-export from a stale seed run ever reintroduces the row.

Consequence for the clamp: `router_outcomes.clamp_inferred_levels`, applied
by `_clamp_inferred` at `scripts/router_server.py:3572`, drops a
scorer-emitted `tool_use` requirement from a foreman tick's inferred matrix
whenever the populated rungs do not express it. The populated `tool_use`
rungs are `[-5, -4, -3, -2, -1, 0, 5]` — there is no lane at levels 1..4, so
an emitted `tool_use >= 1..4` is the cliff artifact the lock describes and is
dropped, not gated on. The inferred-matrix clamp must not treat the
undeclared `tool_use` as an unsupplied requirement for foreman ticks:
tool-calling is table stakes, not a bar.
`tests/test_profile_capability_coverage.py` encodes this as the
documented-unrequired case so the disposition cannot silently rot.
