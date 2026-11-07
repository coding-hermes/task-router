# TR-239: ingress admission sizing is derived from the enabled-lane count

**Date:** 2026-11-07
**Status:** Implemented
**Row:** TR-239

## Decision

The `LaneGuard` / `Admission` bounds in `scripts/router_ingress.py` are no longer fixed
constants: the queue bounds are **derived at construction time from the enabled-lane count**,
with explicit floors (== the pre-TR-239 constants) and explicit hard ceilings. The
`ROUTER_INGRESS_*` env vars still win over the derivation, so an operator pin is never
overridden by arithmetic. A fleet-shaped load test proves a full scheduler tick is SERVED
(100%, zero refusals) under the derived bounds, where the pre-TR-239 shared constants refused
the same tick outright.

## The constants before

| Knob | Env var | Was |
|---|---|---|
| per-lane in-flight | `ROUTER_INGRESS_LANE_MAX_INFLIGHT` | `2` (unchanged) |
| per-lane queue | `ROUTER_INGRESS_LANE_QUEUE_MAX` | `8` (now the derivation floor) |
| per-lane queue wait | `ROUTER_INGRESS_LANE_QUEUE_WAIT_S` | `20.0` (unchanged) |
| global backstop | `ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT` | `0 = off` (unchanged — deriving it would turn a bound ON, a behaviour change for normal traffic) |
| shared in-flight (`Admission`) | `ROUTER_INGRESS_MAX_INFLIGHT` | `4` (now the derivation floor) |
| shared queue (`Admission`) | `ROUTER_INGRESS_QUEUE_MAX` | `16` (now the derivation floor) |
| shared queue wait | `ROUTER_INGRESS_QUEUE_WAIT_S` | `20.0` (unchanged) |

A fleet scheduler runs hundreds of enabled lanes, so an 8-wide per-lane queue and a 4+16
shared pool are sized to DEFEND the router, not to serve our own traffic: the excess is
refused after <=20 s.

## The lane-count source (and why)

`_enabled_lane_count()` resolves at call time (never at import time), then caches one value
per process. Resolution order, first hit wins:

1. **(a) `ROUTER_INGRESS_LANE_COUNT`** (int > 0) — the operator's explicit pin always wins,
   and it is what the load test pins so the test never depends on which registry surface a
   checkout happens to expose.
2. **(b) the router's own registry surface:**
   - `_registry_lane_count()`: the routing registry (`ROUTING_REGISTRY`, else
     `<repo>/registry.json`), table **`projects`** — one row per fleet lane the router knows
     (`router_seed.py` exports it; the scheduler's lane detector keys on the same ids), so its
     row count IS the lane count the router serves. Chosen as the primary discovery source
     because it is the actual lane table, not a proxy. Measured on the canonical registry:
     **969 project rows**.
   - the endpoint registry `data/endpoints.jsonl` via the existing `load_endpoints()` — its
     usable rows are forwarding **targets**, a weak conservative proxy (3 usable rows in-repo)
     and the only surface a fresh clone has.
3. **(c) `LANE_COUNT_FALLBACK = 64`** — a documented conservative constant for a registry-less
   checkout, sized as a mid-size scheduler wave. The fleet really runs hundreds of lanes; 64
   keeps serving without pretending to know the real number.

## The derived formulas

The documented load model: one scheduler tick delivers up to `TICK_TASKS_PER_LANE = 2` tasks
per enabled lane; each task walks up to `TICK_HOPS_PER_TASK = 3` hops (the documented
`max_hops=3` load model), each hop one ingress request, each retryable — up to
`TICK_ARRIVALS_PER_LANE = 6` arrivals per lane per tick, `N x 6` on the shared surface.

```
_derived_lane_queue_max(N)   = clamp( (6 + per_lane_inflight) + ceil(N / 16),  floor 8,   ceiling 64 )
_derived_shared_bounds(N)    = ( clamp( ceil(N / 4),        floor 4,  ceiling 128 ),     # serving width
                                 clamp( N * 6,              floor 16, ceiling 2048 ) )   # waiting room
```

- **Floors** are exactly the pre-TR-239 constants, so nothing shrinks and unsaturated
  behaviour is unchanged.
- **Ceilings** are hard: the lane-count input is clamped to 4096, so a ballooning registry
  can never balloon memory, and the refusal path stays reachable (a burst wider than the
  ceilings still refuses — e.g. `Admission` at the ceiling caps at 128 in flight + 2048
  waiting, refusing arrivals beyond).
- Per-lane queue grows with N (one slot per 16 lanes) because a larger fleet stacks more
  retry backlog per lane; the shared width grows at one slot per 4 lanes so a fleet-sized
  tick is SERVED, not merely stored; the shared queue holds one whole tick (every enabled
  lane arriving together) behind that width.
- `hold()` never blocks longer: the `*_QUEUE_WAIT_S` defaults are untouched (20 s), and the
  widened bounds only ever move a refusal LATER-or-never, never extend a wait beyond the
  unchanged 20 s ceiling.

## Measured (the new tests, `tests/test_proxy_admission_cache.py`)

Load shape: ONE scheduler tick — `ROUTER_INGRESS_LANE_COUNT=48` lanes arriving together on a
barrier, 3 concurrent tasks per lane (one MORE than the model's 2, so the bound must absorb
over-delivery), 3 hops per task, each request retryable with a bounded retry.
Arrivals = 48 x 3 x 3 = **432** (the tasks x hops multiplier is asserted).

| Governor | Bound used | Served | Refusals by class |
|---|---|---|---|
| `LaneGuard` (the Ingress default) | derived per-lane queue = 11 | **432/432 = 100.0%** | none (0 in every class) |
| `Admission` (shared pool) | derived width=12, queue=288 | **432/432 = 100.0%** | none (0 in every class) |
| **Red arm:** `Admission` pinned to the pre-TR-239 constants (4 + 16), slots held the way real model calls hold them | 4 + 16 | **0/72 = 0.0%** of the tick slice | `shared_queue_full=24`, `shared_no_slot_within_wait_s=48` |

Refusal classes (named, counted, never anonymous): `lane_queue_full`, `lane_no_slot_within_wait_s`,
`shared_queue_full`, `shared_no_slot_within_wait_s`, `global_backstop_full`, `circuit_open`.
The module runs in ~1.6 s — threads + the real governor classes, no sleeps, no network, no ports.

## What is explicitly NOT proven

- **A live fleet run.** The 48-lane tick is an in-process shape, not the production `serve`
  door under real scheduler traffic. The canonical registry's 969-row count has not been
  exercised as the live derivation input (the ceilings were verified by unit arithmetic at
  the clamp, not by driving 969 live lanes).
- The per-lane `LaneGuard` leg was served under the pre-TR-239 constants too for THIS tick
  shape (3 concurrent per lane fits 2+8): the derived per-lane bound's value is headroom that
  grows with the fleet, not a rescue of that leg — the red arm proves the shared-pool leg.
- The docs tables in `docs/configuration.md`, `docs/tr236-ingress.md` and
  `docs/router-in-bunker-shortcut.md` still list `8` / `4+16` as the defaults; after this row
  those numbers are the DERIVATION FLOORS (env unset -> derived), and the docs are outside
  this row's file fence, so they were left for the owner's docs pass.
