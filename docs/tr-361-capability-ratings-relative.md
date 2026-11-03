# TR-361 — capability ratings are relative, and the fleet never has a level-5 lane

Handoff note for Bane, written for reading *in* the task-router repo. Everything below is measured
today (2026-11-03) against `registry.json` (generated 11:00Z) and `~/task-router/data/state/unservable-counters.jsonl`.

## 1. The bug that matters right now

`code-factory` foreman failed **6 consecutive ticks** (503/502/429) behind a board of **54 open rows,
all gated, none blocked**. Reason on every one: `failure_reason: unservable-rating`.

The classifier emitted `{agent_tick: 3, delegation: 3, terminal: 2, guard: 2}`. Level 3 on
`agent_tick` requires **min_perf 1.0** — the maximum on the scale. Level 3 on `delegation` also
requires 1.0.

Measured over the registry (2,060 models, 3,940 scored lanes):

| requirement | lanes clearing it |
|---|---|
| `agent_tick >= 3` | **0** |
| `delegation >= 3` | **0** |
| both | **0** |
| full inferred matrix (at3 del3 term2 tool_use2 long_horizon2 guard2) | **0** |

Best lane in the fleet: Kimi K3 at **0.9216 / 0.75** → `agent_tick` level **2**, never 3.
`tool_use` and `long_horizon` have no lane at `>= 2` either.

So the classifier is emitting requirements that are **unreachable by construction**. `unservable` census:
**704 unservable ratings in 24h, 54 distinct matrices**, all `lane_class: primary`.

## 2. Why it is a hard failure instead of a degrade (the broken pipe)

`router_server.py` (~line 4990-5014) names the condition `unservable-rating` and returns the error.
`router_spawn.py` documents the intended behaviour — TR-185 / TR-194 / TR-195 / TR-322 — that the
router *degrades*: pick the best eligible lane, stamp a warning, keep going. That path exists and
is not reached here; the chain comes back empty and the caller gets a 503.

Bane's framing, which is the right one: **failing to pick something is not a reason to give up.**

## 3. The ratings are RELATIVE, not absolute — this is the design intent

Bane, 2026-11-03: the -5..+5 scale is about *relativiveness*. If the best model we have for a
category is the best we have, it should be a **+5** even if in absolute terms it would only be a +2.
+5 = top ~5% *of the lanes we have*; -5 = worst ~5%. The rungs in between are different percentile
cuts. There are 11 rungs (-5..+5).

Consequence: **asking for +5 must always be satisfiable**, because the top lane in every category is
+5 by definition. Today the rungs are absolute min_perf thresholds (`agent_tick` level 3 = 1.0,
level 5 = 1.0 as well), so the top lane scores 2 and the scale's top half is dead. That is the
mismatch to fix: compute rungs as percentiles over the *available* lanes per category, so the
best-of-what-we-have always clears the highest rung.

## 4. The default profile is wrong too

`P0_FORE` declares `agent_tick: 2`. On a relative scale, a default should ask for *nothing special* —
Bane: "default would be nothing on everything, or -1 on everything, so that it gets selected."
A default that asks for level 2 is a default that can starve.

The resolver also clamps inferred categories off under `P0_FORE` ("profile P0_FORE never declares
long_horizon … no lane holds long_horizon=2"), which is the same failure from the other side: a
category nobody can satisfy is being *inferred* into the requirement set.

## 5. What to fix, in order

1. **Degrade, never 503.** Any unservable rating must fall back to the best eligible lane with a
   stamped warning. Acceptance: a resolve of the exact matrix above returns a lane, not an error.
2. **Relative rungs.** Recompute `category_levels` as percentiles over the lanes that exist, so the
   top lane is always rung +5 and the top half of the scale is reachable.
3. **Default = nothing.** `P0_FORE` (and any default) should declare no category above -1, and the
   inferred-matrix clamp should drop categories no lane can satisfy instead of carrying them.
4. **Guard the class.** A regression test that resolves the measured matrix and asserts a lane comes
   back; plus an alarm on `unservable-counters.jsonl` growth (the census already exists — TR-344).

## 6. Acceptance criteria on the board row (TR-361)

See `acceptance_criteria` on TR-361 in `.coding-hermes/board/tasks.jsonl`. Short form: zero
`unservable-rating` rows in a 24h window, a passing test proving the degrade returns a lane, and the
census file flat across a day of live ticking.
