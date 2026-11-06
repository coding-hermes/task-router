# Capability-flag precedence — vision/thinking bars (INT-CI-21)

ch:trace row=INT-CI-21 test=tests/test_routing_quality_corpus.py

## The contradiction

TR-363 moved a level>=1 flag-capability requirement (`vision=1` / `thinking=1`)
from the tier plane to the store's own boolean columns
(`router_spawn._build_chain` flag gate: only an EVIDENCED `True` passes,
`False` = unsupported, `NULL` = unknown/unresearched — the TR-369 three-state
contract). The routing-quality corpus pinned its cases against the tier plane
(wt/TR-350 @ 6b408eb, pre-TR-363), so the arm switch redden two cases:
the boolean admitted lanes the tier plane bars (9 deepseek-family lanes carry
`vision=true` from a models.dev family fill against a `vision tier=-1`
PROFILE_TAGS estimate record) and barred lanes the tier plane admits
(8 null + 6 explicit-false mirror lanes against `vision tier>=3` records).
Probe (pre-fix, router-exact `_build_chain` accounting):
boolean-arm pool = 31 lanes, tier-arm pool = 36 lanes.

## The decided rule

For a level>=1 flag bar, the router's admission arm is the models.jsonl
three-state boolean (probe-proven below). The boolean must MIRROR the tier
plane — the two sources may not disagree about the same bar:

- `model_tier` vision row `tier >= 1`  =>  the lane's `vision` MUST be `true`.
- `model_tier` vision row below the bar (`tier <= 0`, including the blank
  default and estimate/family-alias rows)  =>  the lane's `vision` MUST NOT be
  `true`; the honest value is `null` with a reason note (unresearched is not
  support — fail-closed), not `false` (we do not hold evidence of
  non-support).

The tier plane wins disagreements: it is the fleet's evidence record
(benchmarks, battery overlays, profile-tag estimates), while the boolean is a
published echo. Divergence note: the INT-CI-21 brief suggested the opposite
arm ("boolean wins when tier=-1 AND the alias source has a MEASURED tier");
that rule's own precondition fails for the deepseek family (the alias source
`deepseek-flash` carries `tier_source=estimate`, not measured), and
boolean-wins is unsatisfiable against the pinned corpus (it puts
`deepseek/deepseek-flash` into vision pools the `must_exclude` pins bar and
hijacks the pinned P5_VISION_E2E head).

## The probe (which arm the router actually uses)

Seeded scratch registry from committed data/tables (conftest recipe), then
`router_spawn._build_chain` with `reqs=[('vision',1),('e2e_vision',1)]` under
both arms (today's flag gate vs `_FLAG_CAPS=()` tier simulation):

- `deepseek/deepseek-flash` (`vision=true`, tier=-1): IN the boolean-arm pool,
  OUT of the tier-arm pool with miss `vision level 1 not met (tier -1)`.
- `clinepass/kimi-k3` (`vision=null`, tier=3): OUT of the boolean-arm pool
  with miss `capability vision=1 (unknown (unresearched))`, IN the tier-arm
  pool.

=> admission follows the boolean, not the tier (router_spawn.py flag gate,
TR-363). Post-fix probe: both arms resolve to the SAME 36-lane pool (zero
symmetric difference); `deepseek/deepseek-flash` is absent from both
vision pools (`bool=False tier=False`) — the AC 4 arm.

## The correction (data/tables/models.jsonl, INT-CI-21)

All 23 lanes carry a `note` citing this doc. Verified: a fresh
`router_seed.py` run over the corrected tables regenerates
models/model_perf/model_tier byte-identically (the boolean is not read into
the perf/tier derivation — the tier record is unchanged).

- 9 downgrades `true->null` (deepseek family: commandcode, deepseek official,
  fireworks-ai x3, neuralwatt, opencode-go, openrouter x2).
- 6 upgrades `false->true` (commandcode-2 mirror x4, fireworks-ai short-form
  x2): fill drift — each has a live TRUE twin of the same weights
  (commandcode primaries; `fireworks-ai/accounts/fireworks/models/kimi-k3`)
  plus the `tier>=3` record.
- 8 upgrades `null->true` (kimi-k3 / minimax-m3 deployments): on-row measured
  `perf_e2e_vision` (0.75 / 0.55) and/or the `vision tier=3` record.

## Known hazard (not fixed here)

`router_modelsdev.py` catalog refresh "fills vision only when TRUE" — a future
refresh can re-stamp the 9 downgraded deepseek lanes `null->true` from the
models.dev family payload and re-redden the corpus. The corpus is the tripwire;
a refresh operator must respect this precedence rule (tier record below the bar
=> no boolean true) when filing refresh results.
