# Router-in-the-bunker is a LABELLED SHORTCUT — per-lane isolation, and where it can actually forward

**Status: a shortcut, labelled as one.** The stable path for reaching a remote
agent stays the bus (crier) with the scheduler's dispatch leg
(SCHED-GAP-1667 / 1665 / 1710) and, later, the scheduler's own per-lane and
per-namespace endpoints (SCHED-GAP-1712). Running the router *inside an agent's
bunker* is the fast path so the fleet is not blocked, and it stays legitimate
only as long as it is not the ONLY way to reach a specific agent.

This document records (1) the measurement that refuted the shortcut's original
forwarding destination, (2) the destination that actually exists today, and
(3) the isolation contract the router's forwarding path must satisfy because it
becomes one path for every lane's ticks. Closes SCHED-GAP-1713.

## 1. The destination that does not exist (measured, not assumed)

The first design said: the router runs inside the container and forwards to
"the Hermes gateway". Probed inside a live agent (`helix`, box 02):

| probe | result |
|---|---|
| listening TCP sockets in the container | **none** |
| `hermes` process in the container | **none** |
| gateway state files in the agent home | **none** |
| provisioning log | `gateway setup skipped (no terminal)` |

There is no listener at that address. "Forward it to the gateway" had nowhere
to go — the concept survived, the destination did not.

The per-agent gateways that DO exist are the **box-HOST** units, not container
listeners. Re-verified 2026-10-03 from the control box:

```
rb-01  LISTEN 127.0.0.1:8642  hermes    rb-02  LISTEN 127.0.0.1:8642  hermes
rb-01  LISTEN 127.0.0.1:8643  hermes    rb-02  LISTEN 127.0.0.1:8643  hermes
rb-01  LISTEN 127.0.0.1:8644  hermes    rb-02  LISTEN 127.0.0.1:8644  hermes
rb-01  LISTEN 127.0.0.1:8645  hermes    rb-02  LISTEN 127.0.0.1:8645  hermes
```

Four units per box — but **loopback-bound**, so they are reachable only from
the box itself (an operator session), not from another host. They are not a
remote fan-out destination either.

## 2. The destination that does exist

Driving the agent *inside its container* over `bunker exec`. That is what the
fleet already runs, twice, and both are proven:

* `~/crier-fleet/dispatcher.py` — bus message → `bunker exec <agent> -- sh -c
  'HOME=/home/bunker-<agent> ... hermes chat -q <task> -Q'` → reply delivered on
  the bus. Long-lived poller on the control box (an in-container poller dies when
  the starting exec ends — measured).
* `~/crier-fleet/fleet-gateway-facade.py` — the scheduler's `POST /v1/responses`
  (stream) → per-lane key → the same `bunker exec` drive → SSE answered in the
  shape the scheduler parses. Its own docstring states the finding: *"there is no
  HTTP listener inside an agent container. Work is driven by `bunker exec`, so
  the adapter is a TRANSLATOR, not a tunnel."*

**Design consequence.** The router does not get a new gateway hop. When a
router-in-bunker shape is used it forwards to an endpoint that already exists
(the facade, or a box-local gateway), and the router's own forwarding path is
what must carry the guarantees below.

## 3. The isolation contract (the acceptance criteria)

Because the router becomes one path for every lane's ticks, three properties are
non-negotiable. They are implemented in `scripts/router_ingress.py` and pinned by
tests; the lane is the endpoint id the message addresses.

### (a) It must not be a bottleneck

Admission is **per lane** (`LaneGuard`): each lane gets its own in-flight budget
and its own queue. A lane's saturation can only spend that lane's budget. The
global cap exists only as an **opt-in backstop**
(`ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT`, default `0` = off) and is never what trips
first. The previous single global semaphore was itself the shared blast radius:
one unreachable endpoint parked on every slot refused EVERY lane at once.

### (b) It must fail PER LANE and LOUDLY

Each lane carries a circuit breaker:

```
closed ──(N consecutive lane failures)──▶ open
   ▲                                       │  (cooldown elapses)
   │  (a real answer)                      ▼
   └──────────────────────────────────  half-open ──(probe fails: cooldown ×2)──▶ open
```

* A lane failure is a transport error, a timeout, a 5xx, or an endpoint that
  answers without a terminal answer. **A 4xx is a request problem, not an
  outage** — it never trips a lane.
* While open, messages to that lane are refused **without being forwarded**:
  `endpoint-circuit-open: lane 'helix' tripped after 3 consecutive failures
  (transport-error: URLError…); refusing without forwarding, retry in 30s`.
* Loud means four surfaces, not one: the ledger row (`lane`, `reason`), the bus
  reply envelope (`ok:false`, `reason`), a log line on trip/recovery
  (`LANE helix circuit OPENED …` / `… circuit CLOSED …`), and `GET /health`
  (`lanes.<id>.state` + `consecutive_failures` + `last_failure`).
* The breaker is per lane, so a tripped lane cannot refuse a peer; recovery is a
  half-open probe, and a failed probe re-opens with a doubled (capped) cooldown.

### (c) An inbound burst must not flood a target (the TR-169 lesson)

Per-lane `queue_max` + `queue_wait_s` bound the lane: over the bound the message
is refused immediately with `lane-busy: lane 'x' queue full (…)`, and **nothing
is fired at the target**. A burst addressed at one lane is therefore neither
amplified onto its target nor able to consume a peer's capacity.

## 4. Provisioning a bunker agent that can drive a router (TR-238)

**No bunker image ships a Hermes.** A freshly provisioned agent container has
`git`/`go`/`node`/`python` but no `hermes` binary and no agent home — the first
command an operator runs on the box installs the CLI, and everything after that
assumes it exists. This section records the provisioning path, the proxy
authentication contract for the client that drives it, and the remote-script
convention `bunker exec` requires.

### 4.1 One command: install the Hermes CLI

Inside the agent container:

```bash
uv tool install --python 3.12 hermes-agent
```

This puts three executables on the agent's `PATH` — `hermes`, `hermes-acp`,
`hermes-agent` — with `3.12` supplied to the build (recorded result of this
exact line: `v0.19.0` installed, all three executables present). Without the
Python pin the resolver may pick an interpreter the package cannot build
against; with it, one Hermes per person is one command. The CLI is only the
binary — credentials and per-agent config still live in the agent's own home
(see the bunker docs for `.hermes/` provisioning); this section covers the
router-facing part only.

### 4.2 The proxy client path: literal key vs `api_key_env` (the trap)

When the in-bunker client is configured to call the router's classified proxy
(Path B), its provider entry must carry the router key in one of two forms
(short form: literal api_key in config.yaml works; api_key_env requires the
variable present in the client process environment):

* **`api_key_env`** — the value is the *name* of an environment variable, and
  the client only resolves it if that variable is present **in the client
  process environment** at spawn time. A variable exported in an operator's
  shell, or in a different service unit, is invisible to the agent process.
  "It is set on the box" is not "it is in the process env" — the recorded
  failure of this shape was a client resolving nothing and auth failing with
  the config looking correct.
* **literal `api_key`** — the key value written directly in the client's
  `config.yaml`. Recorded verified: this works with no environment coupling.
  Never commit a real key to any tracked file; the config file lives in the
  agent's private home, not in a repo.

The two forms are mutually exclusive at the same provider entry: if
`api_key_env` names a variable that is not in the process environment, the
client does not silently fall back to a literal — fix the environment or use
the literal form deliberately.

### 4.3 Remote scripts for `bunker exec`: stdin, ASCII, and the `$( )` trap

`bunker exec <agent> --script <file>` mangles `$( )` command substitution
quoting on the way into the container (recorded: the substitution is rewritten
before the shell sees it). The convention the fleet uses instead:

* **Pipe the script on stdin**: `bunker exec <agent> -- sh -s < script.sh` (or
  `... sh -c 'sh -s' < script.sh`). The script bytes reach the container
  unmodified and are interpreted there.
* **Keep the script ASCII-only.** Smart quotes, em dashes, and other non-ASCII
  characters in heredocs have produced quoting/parse errors in the container
  shell; write plain ASCII.
* Record the trap explicitly in any harness that scripts agents: `$( )` inside
  a `--script` payload is not safe. Do command substitution inside the script
  itself (which runs in the container), never rely on the `--script`
  transport to carry it.

### 4.4 Recorded smoke: a fresh agent reaching a router through the tunnel

The following smoke is **recorded history, not re-run for this document**
(witness: the 2026-09-29 provisioning session). It is included as the shape a
repeat must follow — a freshly spawned agent, driven once over `bunker exec`,
answered through the documented tunnel with no hand-holding:

```
hermes chat -q 'Reply with exactly BUNKER-OK-FINAL and nothing else' -Q
exit code: 0
reply: BUNKER-OK-FINAL
ledger: one outcome row appended (single spawn, single answer)
```

The `-q` flag runs the query non-interactively and exits; `-Q` keeps the
session quiet on the bus. Exit 0 plus the exact sentinel plus exactly one
ledger row is the whole assertion. Anyone re-provisioning should reproduce
this and record the new transcript; this document does not claim the external
smoke was re-executed for TR-238.

## 5. Configuration

| Variable | Default | Effect |
|---|---|---|
| `ROUTER_INGRESS_LANE_MAX_INFLIGHT` | `2` | concurrent forwards allowed **per lane** |
| `ROUTER_INGRESS_LANE_QUEUE_MAX` | `8` | how many may wait **per lane** before `lane-busy` |
| `ROUTER_INGRESS_LANE_QUEUE_WAIT_S` | `20` | how long a per-lane waiter may wait |
| `ROUTER_INGRESS_CIRCUIT_FAILURES` | `3` | consecutive lane failures before the circuit opens |
| `ROUTER_INGRESS_CIRCUIT_OPEN_S` | `30` | cooldown before a half-open probe (doubles per failed probe, cap 1 h) |
| `ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT` | `0` | optional global backstop (0 = off; never the first bound) |
| `ROUTER_INGRESS_MAX_INFLIGHT` / `…_QUEUE_MAX` / `…_QUEUE_WAIT_S` | `4` / `16` / `20` | the legacy **shared** pool (`Admission`) — kept for callers that explicitly want one global cap |

## 6. Evidence

Hermetic pins in `tests/test_ingress.py` (11 new tests, 47 in the file):

* `test_a_shared_pool_is_the_blast_radius_we_removed` — the legacy shared pool
  refuses a HEALTHY lane; this is the defect the per-lane guard exists to fix.
* `test_a_hung_lane_cannot_refuse_a_peer`, `test_a_tripped_lane_does_not_refuse_a_peer`
  — a saturated/tripped lane leaves its peer `ok` and `closed`.
* `test_lanes_run_concurrently_a_slow_lane_holds_only_its_own_slot` — real
  threads: a slow lane holds only its own slot while a peer is served.
* `test_a_lane_that_keeps_failing_trips_and_is_refused_fast` — the refusal is
  fast, the endpoint is not touched, and the trip is loud (factory counter).
* `test_a_half_open_probe_closes_the_circuit_loudly`,
  `test_a_failed_half_open_probe_reopens_with_backoff`.
* `test_a_4xx_is_a_request_problem_not_a_lane_outage`.
* `test_a_burst_is_refused_before_it_reaches_the_target` — 12 messages at a
  saturated lane: all refused, zero requests reached the target.

Run them with `make test` (or `pytest tests/test_ingress.py`).

Live transcript through the real push door (a genuinely closed port beside a real
HTTP target, including the 6-message burst that reached the target exactly once):
`docs/evidence/router-in-bunker-isolation-2026-10-03.md`.

## 7. Known gaps (not silently absent)

1. **The actually-usable remote path is still a single process.** The
   facade (`fleet-gateway-facade.py`, one port) and the dispatcher (one loop)
   drive `bunker exec` for every agent. Per-lane isolation in the router bounds
   what the router fires at them, but the facade/dispatcher themselves have no
   per-lane budget/breaker yet — if the scheduler is cut over to them, THAT is
   where the remaining shared blast radius lives (SCHED-GAP-1712 / 1665).
2. **The box-host gateways are loopback-bound.** Addressing them requires an SSH
   tunnel or going through the facade; this document records the measurement, it
   does not add the tunnel.
3. **Lane = the addressed endpoint id.** Which lane gets worked *when* remains
   the scheduler's decision; the router enforces admission and isolation, it does
   not schedule.
