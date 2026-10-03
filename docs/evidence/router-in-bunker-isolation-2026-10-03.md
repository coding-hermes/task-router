# Per-lane isolation, live (SCHED-GAP-1713) — 2026-10-03

Transcript of the demo that exercises the real push door (`router ingress
serve`) against a real closed port and a real local HTTP target. Nothing here is
a unit stub: the "dead lane" is `127.0.0.1:9` (kernel refuses the connection) and
the target counts the requests that actually arrive.

Setup: two lanes in a temp registry — `dead-lane` (`http://127.0.0.1:9`) and
`live-lane` (a local HTTP target on `:9412`, 1.5 s per request) — with
`ROUTER_INGRESS_LANE_MAX_INFLIGHT=1`, `ROUTER_INGRESS_LANE_QUEUE_MAX=0`,
`ROUTER_INGRESS_CIRCUIT_FAILURES=3`, `ROUTER_INGRESS_CIRCUIT_OPEN_S=60`.

## Phase 1 — a lane that keeps failing trips; a peer keeps working

```
dead-lane #1 -> {"ok": false, "outcome": "failed",   "reason": "transport-error: URLError (<urlopen error [Errno 111] Connection refused>)"}
dead-lane #2 -> {"ok": false, "outcome": "failed",   "reason": "transport-error: URLError (<urlopen error [Errno 111] Connection refused>)"}
dead-lane #3 -> {"ok": false, "outcome": "failed",   "reason": "transport-error: URLError (<urlopen error [Errno 111] Connection refused>)"}
dead-lane #4 -> {"ok": false, "outcome": "refused",  "reason": "endpoint-circuit-open: lane 'dead-lane' tripped after 3 consecutive failures (transport-error: URLError (<urlopen error [Errno 111] Connection refused>)); refusing without forwarding, retry in 60s"}
live-lane #1 -> {"ok": true,  "outcome": "ok",       "reason": "extracted from json:choices.0.message.content", "seconds": 1.501}
```

The peer was served **while `dead-lane` was open** — the failure did not become
the peer's failure. Target hits after phase 1: `{"hits": 1}` (only the live
request reached it; the four messages to the dead lane never arrived anywhere).

## Phase 2 — a 6-message burst at one lane must not flood the target

Six concurrent posts to `live-lane` (per-lane budget 1, queue 0):

```
m-live-lane-2 -> refused  lane-busy: lane 'live-lane' queue full (0 waiting, 1 in flight)
m-live-lane-3 -> refused  lane-busy: lane 'live-lane' queue full (0 waiting, 1 in flight)
m-live-lane-4 -> refused  lane-busy: lane 'live-lane' queue full (0 waiting, 1 in flight)
m-live-lane-5 -> refused  lane-busy: lane 'live-lane' queue full (0 waiting, 1 in flight)
m-live-lane-6 -> refused  lane-busy: lane 'live-lane' queue full (0 waiting, 1 in flight)
m-live-lane-7 -> {"ok": true, "outcome": "ok", "reply": "LIVE-OK-2"}
```

Target hits after the 6-message burst: `{"hits": 2}` — **exactly one** request
arrived, so five were refused instead of amplified (the TR-169 lesson).

## `/health` — which lane is down, per lane

```json
{"status": "ok", "ingress": "task-router",
 "admission": {"lanes": {
    "dead-lane": {"state": "open", "consecutive_failures": 3, "total_failures": 3,
                  "failure_threshold": 3, "open_count": 1,
                  "last_failure": "transport-error: URLError (<urlopen error [Errno 111] Connection refused>)",
                  "last_state_change": "opened"},
    "live-lane": {"state": "closed", "consecutive_failures": 0, "total_ok": 2,
                  "rejected": 5, "open_count": 0}},
  "trips": 1, "recoveries": 0, "per_lane_max_inflight": 1, "per_lane_queue_max": 0,
  "circuit_failure_threshold": 3, "circuit_open_s": 60.0, "global_max_inflight": 0},
 "endpoints": ["dead-lane", "live-lane"]}
```

## Loud: the log line and the ledger

```
LANE dead-lane circuit OPENED: 3 consecutive failures, last=transport-error: URLError
(<urlopen error [Errno 111] Connection refused>); cooldown 60s — refusing without forwarding until then
```

```
"lane": "dead-lane"  "outcome": "failed"   "reason": "transport-error: ..."
"lane": "dead-lane"  "outcome": "failed"   "reason": "transport-error: ..."
"lane": "dead-lane"  "outcome": "failed"   "reason": "transport-error: ..."
"lane": "dead-lane"  "outcome": "refused"  "reason": "endpoint-circuit-open: lane 'dead-lane' tripped after 3 consecutive failures (…)"
"lane": "live-lane"  "outcome": "ok"       "reason": "extracted from json:choices.0.message.content"
"lane": "live-lane"  "outcome": "refused"  "reason": "lane-busy: lane 'live-lane' queue full (0 waiting, 1 in flight)"  (×5)
"lane": "live-lane"  "outcome": "ok"       "reason": "extracted from json:choices.0.message.content"
```

Hermetic pins for the same invariants (no network): the 12 new tests in
`tests/test_ingress.py` — see `docs/router-in-bunker-shortcut.md` § 5.
