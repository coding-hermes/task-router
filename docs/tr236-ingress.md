# Crier ingress — the bus→router door (TR-236)

The router has always been *called*: two HTTP doors (the classified proxy on
`:9391`, the read API on `:9092`) and no way to be told anything. A Crier bus
message addressed to it sat in an inbox forever. This document is the contract
for the inbound leg — and for the part that is genuinely new work, the
**translation** from a bus message into the request a target endpoint's protocol
requires, with the model choice left to the router.

Implementation: `scripts/router_ingress.py` (stdlib only). Declarations:
`data/endpoints.jsonl`. Tests: `tests/test_ingress.py`.

## The two doors

Both doors share one `Ingress` core; only the transport differs.

| Door | Command | Why it exists |
|---|---|---|
| **poll** | `router_ingress.py poll` | Drains the router's bus inbox(es) and keeps working when the box is briefly offline — the durable inbox redelivers, and a leased-but-unfinished message is not acked. |
| **serve** | `router_ingress.py serve --token-file … [--require-sig]` | An authenticated inbound HTTP endpoint for a push (crier webhook/subscription). Refuses unsigned/unauthenticated callers by name, and reports the reply back to the bus itself. |
| **forward** | `router_ingress.py forward --message-file m.json` | One message, no bus. This is what the tests and the offline debugging use. |

In poll mode the ingress identity is a real bus participant: it retrieves with
its own signed request (X-Agent-ID/Ts/Sig) and delivers the reply with
`sender=<its id>`. In push mode the same identity delivers the reply, so the two
doors leave the same trace on the bus.

## The endpoint declaration is data, not code

One JSON object per line in `data/endpoints.jsonl`. Adding a target that speaks
an existing protocol touches no code.

```
id         stable name the bus message addresses         (required)
protocol   one of the closed set below                   (required)
address    base URL, or a bus agent id for bus-native    (required)
auth       a REFERENCE to a secret, never the secret     (required)
reply      how to extract the answer (see below)         (required)
timeout_s  the bound for one request                     (required, > 0)
model      "router" (default) or "none"                  (optional)
```

`auth` reference forms: `none`, `env:NAME`, `file:PATH`,
`env-file:PATH:NAME`. A reference that cannot be resolved is a **named refusal**
(`auth-unresolved: …`) and the doomed call is never fired — there is no
"proceed unauthenticated, it usually works" path.

`model` says who chooses the pair. `router` (the default) asks
`router_spawn.py` for the head of the gated, price-ordered chain for the profile
the *message* named, and injects it. `none` sends no model field at all. Either
way the message can never choose a model itself.

### Closed sets

| `protocol` | Request | Reply shape |
|---|---|---|
| `hermes-gateway` | `POST <address>/v1/responses`, `{"input": …, "stream": true}`, `X-Hermes-Session-Key` | SSE; the terminal `response.completed` envelope |
| `openai-compatible` | `POST <address>/v1/chat/completions` | JSON |
| `anthropic-messages` | `POST <address>/v1/messages` | JSON |
| `webhook` | `POST <address>` with the raw payload | none — the answer arrives later as its own bus message |
| `bus-native` | no HTTP: another participant on the same bus | — |

| `reply` rule | Meaning |
|---|---|
| `sse-last-message` | read the SSE stream to its terminal event and take the text |
| `json:<dotted.path>` | a JSON body with a known field, e.g. `json:choices.0.message.content` |
| `webhook-separate` | the endpoint acknowledges only; the answer comes later |
| `none` | fire and forget, and the absence of an answer is stated rather than faked |

A stream that ends without a terminal event is **not** a partial answer: it is an
`unservable` outcome. `"the HTTP call returned 200"` is not `"the agent
answered"`, so the extraction rule is part of the protocol, not an afterthought.

### Refusal rules (the part that matters)

An endpoint that cannot declare the required fields is refused, at load time and
again at use time, with a named reason. **No guessed transform, and above all no
silent default to Hermes.** The vocabulary:

```
unknown-endpoint: 'x' is not declared (known: …)
endpoint-incomplete: missing timeout_s; unknown protocol 'grpc'
address-unresolvable: endpoint 'x' declares no address
auth-unresolved: API_SERVER_KEY not set in /home/…/.hermes/.env
endpoint-unsupported-protocol: 'grpc'
no-prompt: payload carried none of prompt/text/task/message/body/input
timeout-not-declared: endpoint 'x' has no bound
lane-busy: lane 'x' queue full (8 waiting, 2 in flight)
endpoint-circuit-open: lane 'x' tripped after 3 consecutive failures (…); retry in 30s
overloaded: global backstop full (max N in flight, all lanes)
unauthorized: signature does not verify for 'task-router'
duplicate: idempotency_key already forwarded
```

Every one of them produces a reply envelope on the bus (never silence) and a
ledger row.

## Translation: address + payload → prompt + session + model

The message shape overlaps with what the fleet already emits (the old
`dispatcher.py` reads `task`/`text`/`prompt`/`message`/`body`, so this reader
accepts the same), plus the addressing the new pipeline needs:

```json
{"endpoint": "hermes-gateway-local",     // or "to"/"target"
 "task": "…",                            // prompt|text|task|message|body|input
 "session": "tick-<id>",                 // the gateway session key
 "profile": "P1_CODING",                 // what the ROUTER routes on
 "reply_to": "orchestrator",             // default: the message's sender
 "system": "…",                          // optional
 "timeout_s": 120,                       // optional narrowing of the bound
 "idempotency_key": "…"}                 // optional; a duplicate is not re-run
```

The envelope may carry `id` and `sender`; the ingress never invents either. The
reply is pairable from the bus alone:

```json
{"in_reply_to": "<inbound message id>", "endpoint": "<the endpoint that SERVED it>",
 "endpoint_requested": "…", "protocol": "…", "transform": "…", "ok": true,
 "outcome": "ok", "reason": "extracted from output", "session": "…",
 "model_chosen": "…", "model_served": "…", "tokens_in": 51236, "tokens_out": 8,
 "cost_usd": null, "seconds": 2.487, "reply": "…"}
```

`endpoint` records what **served** the request and `model_served` what actually
ran; `model_chosen` is what the router picked. The configured-vs-served split is
the difference between a ledger that can diagnose a misroute and one that agrees
with itself.

## The ledger

One row per attempt, appended to `$ROUTER_STATE_DIR/ingress-ledger.jsonl`
(`ROUTER_INGRESS_LEDGER` overrides): `ts, ingress, inbound_id, sender, session,
endpoint_requested, endpoint_served, protocol, transform, model_chosen,
model_served, tokens_in, tokens_out, cost_usd, ok, outcome, reason, seconds,
idempotency_key, http_status, null_reasons`. Every null can carry a reason in
`null_reasons` (e.g. `model_served: "not reported by endpoint"` — the Hermes
gateway answers `model: "Hermes Agent"`, a product name, not a model id, and
recording that as a served model would be a fabricated billing fact).

## The bounds

* **Per request**: `timeout_s` from the declaration (narrowed, never widened, by
  the message) — an endpoint with no bound is refused.
* **Admission is PER LANE** (SCHED-GAP-1713): each addressed endpoint gets its own
  in-flight budget and queue (`ROUTER_INGRESS_LANE_MAX_INFLIGHT` 2 +
  `ROUTER_INGRESS_LANE_QUEUE_MAX` 8 waiting + `ROUTER_INGRESS_LANE_QUEUE_WAIT_S`
  20). Over the bound ⇒ `lane-busy`, refused with the reason, never an unbounded
  wait (the TR-169 lesson: an ingress must not amplify a burst onto its target).
  A lane's saturation can never consume a peer's capacity. The legacy **shared**
  pool (`ROUTER_INGRESS_MAX_INFLIGHT` 4 + `…_QUEUE_MAX` 16 + `…_QUEUE_WAIT_S` 20,
  class `Admission`) still exists for callers that explicitly want one global cap;
  the default is per-lane, and a global backstop is opt-in
  (`ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT`, 0 = off).
* **A lane that keeps failing trips its own circuit** (SCHED-GAP-1713b): after
  `ROUTER_INGRESS_CIRCUIT_FAILURES` (3) consecutive lane failures — transport
  error, timeout, 5xx or an answer with no terminal event; a 4xx deliberately does
  NOT count — the lane is refused fast and loudly (`endpoint-circuit-open`, in the
  ledger, the bus reply and a log line) without firing at the target, for
  `ROUTER_INGRESS_CIRCUIT_OPEN_S` (30s). A half-open probe then tests recovery; a
  failed probe re-opens with a doubled (capped at 1 h) cooldown. `GET /health`
  reports every lane's `state`, `consecutive_failures` and `last_failure`.
* **Idempotency**: a re-delivered `idempotency_key` is answered `duplicate` and
  never forwarded twice.

The design record for the isolation contract — and for the measured finding that
an agent container has no gateway to forward to — is
`docs/router-in-bunker-shortcut.md`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `ROUTER_INGRESS_ENDPOINTS` | `data/endpoints.jsonl` | the endpoint registry |
| `ROUTER_INGRESS_LEDGER` | `$ROUTER_STATE_DIR/ingress-ledger.jsonl` | the audit ledger |
| `ROUTER_INGRESS_LANE_MAX_INFLIGHT` | `2` | concurrent forwards **per lane** |
| `ROUTER_INGRESS_LANE_QUEUE_MAX` | `8` | how many may wait **per lane** before `lane-busy` |
| `ROUTER_INGRESS_LANE_QUEUE_WAIT_S` | `20` | how long a per-lane waiter may wait |
| `ROUTER_INGRESS_CIRCUIT_FAILURES` | `3` | consecutive lane failures before the circuit opens |
| `ROUTER_INGRESS_CIRCUIT_OPEN_S` | `30` | cooldown before a half-open probe (doubles per failed probe, cap 1 h) |
| `ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT` | `0` | optional global backstop (0 = off, never the first bound) |
| `ROUTER_INGRESS_MAX_INFLIGHT` | `4` | legacy **shared** pool (`Admission`) only |
| `ROUTER_INGRESS_QUEUE_MAX` | `16` | legacy shared pool: how many may wait |
| `ROUTER_INGRESS_QUEUE_WAIT_S` | `20` | legacy shared pool: wait bound |
| `ROUTER_INGRESS_TOKEN` | — | bearer for the `serve` door |
| `ROUTER_INGRESS_BUS_IDS` | `task-router` | inbox identities this ingress drains |
| `ROUTER_INGRESS_BUS_URL` | `http://100.97.236.14:8767` | the bus (`CRIER_URL` also read) |
| `ROUTER_INGRESS_BUS_TOKEN_FILE` | `~/.hermes/secrets/crier-fleet.token` | bus bearer (`CR_AUTH_TOKEN` also read) |
| `ROUTER_INGRESS_KEY_DIR` | `~/crier-fleet/keys` | `<identity>.key` files |
| `ROUTER_INGRESS_RESOLVE` | `scripts/router_spawn.py` | the model resolver |
| `ROUTER_INGRESS_RESOLVE_TIMEOUT_S` | `30` | resolver subprocess bound |
| `ROUTER_INGRESS_MAX_TOKENS` | `4096` | `max_tokens` for anthropic-messages |
| `CRIER_CLIENT_PATH` | `~/crier/clients/python` | the fleet's bus client (not vendored) |

## Verified live (2026-10-02, control box)

`docs/evidence/tr236-ingress-live-2026-10-02.md` holds the transcript. In short:

* bus message → ingress → **Hermes gateway `/v1/responses` SSE** → reply on the
  bus, `TR236-INGRESS-OK`, 3.2 s, `in_reply_to` matching the inbound id, the
  inbound message acked (inbox back to 0/0);
* with a profile on the message the router's pair is what runs:
  `model_chosen = model_served = qwen/qwen3.7-flash:free`;
* through the classified proxy (`openai-compatible`, the card's "POST to the
  local proxy"): `model_chosen = qwen/qwen3.7-flash:free` while
  `model_served = meta/muse-spark-1.3-contributor:free` — the configured-vs-
  served split doing exactly the job it exists for;
* the non-Hermes shape (`webhook`) exercised against a live sink: the transform
  arrived, and the reply states honestly that no inline answer exists;
* the push door: no token → 401, token but unsigned → 401, tampered signature →
  401 `signature does not verify for 'task-router'`, valid token + valid bus
  signature → forwarded, and the reply delivered on the bus
  (`bus_delivery: ok:orchestrator`).

## Known gaps (not silently absent)

1. **A bus payload containing `"prompt":` (or `"instructions":`, `"system":`,
   `"tools":`, `"schema":`) is refused by crier's own guard** with 403
   `GUARD_BLOCKED` (the `control_keys` prematch pattern classifies it as a
   structured-object injection attempt). Measured 2026-10-02. This is why the
   fleet's dispatcher reads `task` first, and why the examples here address the
   prompt as `task`. The ingress accepts the other keys for HTTP push and for
   file/forward input, but a *bus* sender must avoid `"prompt":` as a JSON key.
2. `anthropic-messages` is declared, transformed and unit-tested, but no live
   Anthropic-compatible endpoint exists on this box, so it is not proven
   end-to-end. The webhook shape carries the "non-Hermes is real" burden.
3. The direct `hermes-gateway` hop is bounded by `timeout_s` (a wall), not by the
   proxy's idle watch. For a long, slow-but-alive turn prefer the
   `router-proxy-local` endpoint (the proxy owns the idle semantics and the
   ladder).
4. This is the *ingress*. The scheduler's dispatch decision (which lane goes to
   which agent) is SCHED-GAP-1665 and is not implemented here; a dispatch that
   carries a task id would still be wrong.
